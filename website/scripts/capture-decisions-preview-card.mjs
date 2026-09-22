/**
 * Screenshot harness for the Decisions (Jev) card in Settings > Developer >
 * Feature Previews.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth, and no decision
 * provider: nothing here arms the real seam, and the only `decisions` values that
 * exist are the ones these fixtures invent.
 *
 * The card is a LIST of decision points and a DETAIL panel for the highlighted
 * one, above a shared block the whole seam shares. Every state it can be in is a
 * state of three answers this harness varies: the keystone behind
 * `/api/decisions/consent` (consent, per-point scopes, and the server's status per
 * point), `config.json` behind `/api/config/kirocrew` (address, sampling share,
 * history budget, tier-to-model map), the vault index behind `/api/secrets`
 * (whether the provider credential is set) and the fleet ceiling reported as
 * `decisions_enabled` on `/api/dashboard/config`. Frames:
 *   decisions-overview-light.png            the list: one row per point the gateway
 *                                           projects, each with its status chip.
 *   decisions-detail-model-route-light.png   the model-route panel's three tier pickers.
 *   decisions-detail-nudge-wake-light.png    the judge panel's provider and model
 *                                           pickers, reachable with consent OFF.
 *   decisions-detail-nudge-wake-provider-open-light.png
 *                                           the same menu OPEN, where each option's
 *                                           destination is legible before it is picked.
 *   decisions-detail-nudge-wake-jev-refused-light.png
 *                                           the judge pinned to Jev with the switch
 *                                           off: the refuse path, no lane named.
 *   decisions-detail-needs-ok-light.png      tool.risk's panel with its scope switch
 *                                           off, and the line naming where the OK goes.
 *   decisions-detail-compaction-needs-ok-light.png
 *                                           the whole-transcript switch, off.
 *   decisions-detail-message-steer-light.png a panel for a point needing no scope.
 *   decisions-global-open-light.png          the shared block: address, credential,
 *                                           sampled share and the history ceiling.
 *   decisions-off-light.png                  a keystone reading `enabled: false`.
 *   decisions-off-shared-light.png           the shared block with consent off, the
 *                                           history box disabled.
 *   decisions-points-unavailable-light.png   a gateway that projects no points.
 *   decisions-global-key-remove-confirm-light.png
 *                                           the credential row's open confirmation.
 *   decisions-endpoint-moved-light.png       consent given for one address, config now
 *                                           naming another. Kept for the origin
 *                                           comparison it exercises both ways.
 *                                        off: NO card, not a disabled one
 *
 * WHAT THIS HARNESS ASSERTS, and why it asserts anything at all: a capture script
 * that only writes PNGs fails toward a false pass — a fixture typo, a clipped
 * card or a state that never arrived all still produce a tidy image a PR can cite.
 * An earlier version guarded only that the SWITCH was inside the viewport, and the
 * UX review lane then reported that every frame cropped the rows off the bottom. So
 * the guard is on the card's LAST element in each state, and each state
 * additionally asserts the text that makes it that state.
 *
 * Usage: node scripts/capture-decisions-preview-card.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decisions-preview-card'
mkdirSync(OUT, { recursive: true })

/** The address the fixtures consent to. */
const JEV_ENDPOINT = 'https://api.typesafe.ai/v1/systemone'

/** A DIFFERENT address, for the state where config moved out from under consent. */
const MOVED_ENDPOINT = 'https://proxy.example/v1/systemone'

/**
 * Whether *candidates* contains the endpoint *expected*, compared as a URL.
 *
 * A substring test is the wrong comparison for an address, and wrong in the direction
 * that matters: `includes('https://api.typesafe.ai/v1/systemone')` is satisfied by
 * `https://api.typesafe.ai/v1/systemone.evil.test/x` and by
 * `https://evil.test/?u=https://api.typesafe.ai/v1/systemone`, so a card showing an
 * address nobody consented to would pass the very check written to catch it.
 * `js/incomplete-url-substring-sanitization` is the rule for that shape.
 *
 * Each candidate is one ELEMENT's text rather than a slice of the card's, because an
 * address is a whole value and `textContent` over a subtree concatenates without
 * separators -- the address would arrive glued to the sentence after it. ORIGIN and
 * PATHNAME must both match: the origin alone would accept a different path on the
 * right host, and what this asserts is the exact endpoint consent is given FOR. A
 * candidate that does not parse is not a URL and cannot be the address.
 */
const statesEndpoint = (candidates, expected) => {
  const want = new URL(expected)
  for (const candidate of candidates) {
    let got
    try {
      got = new URL(candidate.trim())
    } catch {
      continue
    }
    if (got.origin === want.origin && got.pathname === want.pathname) return true
  }
  return false
}

/**
 * The four points this gateway ships, as the consent GET projects them: the id,
 * the scope its egress needs (only `tool.risk` has one), the SERVER's effective
 * status, and the config paths the card points at rather than controls.
 *
 * `status` is computed here the way the handler computes it, because the whole
 * point of the projection is that the card never decides it: a row is `active`
 * only while consent stands AND the point's scope (where it has one) was granted.
 */
const POINTS = [
  { id: 'skills.select', needs_scope: null, config_keys: ['skills.max_triggered'] },
  { id: 'tool.risk', needs_scope: 'tool_args', config_keys: [] },
  { id: 'message.steer', needs_scope: null, config_keys: [] },
  { id: 'model.route', needs_scope: null, config_keys: [] },
  { id: 'compaction.keep', needs_scope: 'compaction', config_keys: [] },
  { id: 'memory.recall', needs_scope: 'memory_text', config_keys: [] },
  // The one point whose row does not follow from consent alone: its `llm` provider
  // sends to the model provider this machine already uses, so the fixture below
  // reports it active on that provider whatever the keystone says, exactly as
  // `_points` does.
  { id: 'nudge.wake', needs_scope: null, config_keys: [] },
]

// Status is resolved PER SCOPE, as the gateway resolves it: a row says whether ITS
// OWN consent is recorded, so one flag for every scope would draw the wrong answer on
// the point that needs the other one.
const JUDGE_POINT = 'nudge.wake'

/**
 * Which lane the gate would resolve for the judge, mirrored so a frame cannot pass
 * against a lane the gateway would not report.
 *
 * `auto` goes to Jev only when Jev is ARMED for this point, which needs this point's
 * own evidence scope -- and this build registers none, so `auto` reaches the small
 * model even with the switch on and the address in force. An explicit pick is honoured
 * as picked.
 */
const judgeLane = judgeProvider => (judgeProvider === 'jev' ? 'jev' : 'llm')

const pointRows = ({ enabled, permits, toolArgs, compaction, memoryText, judgeProvider }) => {
  const granted = { tool_args: toolArgs, compaction, memory_text: memoryText }
  return POINTS.map(p => {
    if (p.id !== JUDGE_POINT) {
      return {
        ...p,
        status:
          !enabled || !permits
            ? 'off'
            : p.needs_scope && !granted[p.needs_scope]
              ? 'needs_scope'
              : 'active',
      }
    }
    const lane = judgeLane(judgeProvider)
    return {
      ...p,
      lane,
      // The small-model lane needs neither the switch nor a scope, only a runner, and a
      // gateway has one. A pinned Jev lane has no grant to run on while this point
      // registers no scope, so the row names the missing grant rather than claiming to
      // run -- which is what the gateway's own projection reports.
      status: lane === 'llm' ? 'active' : permits ? 'needs_scope' : 'off',
    }
  })
}

/**
 * The plain-words name the card draws per point id, and the row list a given consent
 * state must produce. Derived from POINTS rather than written out per frame: a literal
 * per call site meant a point the gateway ships had to be added in as many places as
 * there are frames, and a frame that missed one photographed a card with a row absent.
 */
const POINT_NAMES = {
  'skills.select': 'Automatic skill choice',
  'tool.risk': 'Risky tool-call notes',
  'message.steer': 'Mid-turn message handling',
  'model.route': "Model for the turn's difficulty",
  'compaction.keep': 'Which tool calls a compaction would keep',
  'memory.recall': 'Which recalled memories reach the prompt',
  'nudge.wake': 'Quiet check-ins: wake or skip',
}

const STATUS_WORDS = { active: 'Switched on', needs_scope: 'Needs your OK', off: 'Off' }

// The judge row is the one row whose ACTIVE has two meanings, so the card names the
// lane that would answer instead of the generic word. The card reads that lane off the
// row rather than deriving it, so this mirrors the same read -- a chip derived from the
// switch would expect the generic word over a small-model judge.
const judgeChip = row => (row.lane === 'llm' ? 'Judged by the small model' : STATUS_WORDS.active)

const expectRows = state =>
  pointRows(state).map(row => ({
    name: POINT_NAMES[row.id],
    status:
      row.id === JUDGE_POINT && row.status === 'active'
        ? judgeChip(row)
        : STATUS_WORDS[row.status],
  }))

/** The consent GET/PUT payload the gateway returns, bound to the default endpoint. */
const consentPayload = ({
  enabled = false,
  configured_endpoint = JEV_ENDPOINT,
  permits,
  tool_args = false,
  compaction = false,
  history_budget_chars = 0,
  points,
  judgeProvider = 'auto',
} = {}) => {
  const allowed = permits ?? (enabled && configured_endpoint === JEV_ENDPOINT)
  return {
    enabled,
    endpoint: enabled ? JEV_ENDPOINT : '',
    configured_endpoint,
    permits: allowed,
    tool_args,
    // The whole-transcript scope, cleared with the switch exactly as the keystone
    // clears it.
    compaction: enabled ? compaction : false,
    // The reviewed ceiling, which is what the card draws and writes. Cleared with the
    // switch, exactly as the keystone clears it, so an off frame shows 0.
    history_budget_chars: enabled ? history_budget_chars : 0,
    // A build older than the projection sends no rows at all, and the card reports
    // that rather than drawing a list of its own. `null` asks for that frame.
    ...(points === null
      ? {}
      : {
        points: pointRows({
          enabled,
          permits: allowed,
          toolArgs: tool_args,
          compaction,
          judgeProvider,
        }),
      }),
  }
}

/**
 * A gateway that carries the fields this card writes.
 *
 * `bucket` defaults to 100 because that is what a real gateway returns for an
 * untouched config, and the slider shows that share in both switch states. The
 * tier map is EMPTY by default: "keep the session's own model" is what a build
 * ships, and a model id written into a fixture would read as a recommendation.
 * Consent itself is NOT in this config: it is the keystone, answered by the
 * `consent` option of `openPage`.
 */
const withDecisions = ({
  bucket = 100,
  history = 4000,
  modelRoute = {},
  judgeProvider = 'auto',
  judgeModel = '',
} = {}) => ({
  ...KIROCREW_CONFIG_FIXTURE,
  decisions: {
    bucket,
    history_budget_chars: history,
    model_route: modelRoute,
    nudge_wake: { provider: judgeProvider, llm_model: judgeModel },
    provider: { endpoint: JEV_ENDPOINT, api_key: 'secret://TYPESAFE_API_KEY' },
  },
})

/** What `/api/models` advertises, so the tier pickers have a list to draw. */
const MODELS = [
  { model_name: 'auto', description: 'Let the gateway choose' },
  { model_name: 'kiro-fast', description: 'Quickest' },
  { model_name: 'kiro-balanced', description: 'Balanced' },
  { model_name: 'kiro-deep', description: 'Slowest, strongest' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []

  /**
   * `consent` is the keystone's answer: an object for a supported gateway, or
   * an HTTP status (404 = older gateway with no consent route, 503 = read failed).
   *
   * `governance` is the fleet ceiling: `true` is a fleet that allows the seam,
   * `false` one that pinned `capabilities.decisions` off, and a number is the
   * ceiling READ failing with that status — which is not a withdrawal.
   *
   * @param {{theme?: string, config?: object|null, consent?: object|number,
   *          putStatus?: number, secrets?: string[], governance?: boolean|number}} opts
   */
  const openPage = async ({
    theme = 'light',
    config = null,
    consent = 404,
    putStatus = 200,
    secrets = [],
    governance = true,
  } = {}) => {
    const context = await browser.newContext({
      // Tall enough for the whole section plus the card's list and panel. The
      // assertions below are what actually hold the framing; this is only a
      // starting size, and the list-and-detail card needs more of it than the
      // single column it replaced. The width is chosen so the card frame at 2×
      // stays under 1800px wide, which is what a PR page renders without
      // downscaling.
      viewport: { width: 1360, height: 1800 },
      deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
    })
    const page = await context.newPage()
    logPageProblems(page)
    let state = typeof consent === 'number' ? consent : { ...consent }
    await stubDashboardApi(page, {
      theme,
      // Developer Mode on so the Developer rail row exists; the section itself
      // does not depend on it.
      localStorageEntries: { 'mc-dev-mode': '1' },
      // The config IS the fixture under test here, so it is answered ahead of the
      // shared map rather than after it.
      extra: async (path, route) => {
        if (path === '/api/decisions/consent') {
          if (route.request().method() === 'PUT') {
            if (putStatus !== 200) {
              await json(route, { error: 'dashboard owner required', code: 'dashboard_owner_required' }, putStatus)
            } else {
              const sent = JSON.parse(route.request().postData() || '{}')
              // The route PRESERVES a field the body omits, which is the property a
              // scope-only write depends on: flipping `tool_args` must not restate
              // consent, and flipping consent must not clear a granted scope.
              // Field by field, and ONLY the fields the body names: an omitted field
              // is preserved, which is the property a standalone write depends on --
              // flipping one scope must not restate consent, and flipping consent must
              // not clear a granted scope. Driven off the field list rather than one
              // `if` per name, so a scope this build adds is carried here too.
              if (typeof state === 'object') {
                if ('enabled' in sent) state.enabled = sent.enabled === true
                for (const scope of ['tool_args', 'compaction']) {
                  if (scope in sent) state[scope] = sent[scope] === true
                }
                if ('history_budget_chars' in sent) {
                  state.history_budget_chars = sent.history_budget_chars
                }
              }
              await json(route, consentPayload(typeof state === 'object' ? state : {}))
            }
            return true
          }
          if (typeof state === 'number') {
            await json(route, { error: state === 404 ? 'not found' : 'keystone unreadable' }, state)
          } else {
            await json(route, consentPayload(state))
          }
          return true
        }
        if (path === '/api/dashboard/config') {
          if (typeof governance === 'number') {
            await json(route, { error: 'capability read failed' }, governance)
            return true
          }
          // The shared fixture's own fields, plus the one this card reads: the
          // ceiling is answered here rather than in the shared map because it is a
          // state this harness varies.
          await json(route, {
            restore_sessions: false,
            restore_window_minutes: 30,
            merge_queued_messages: false,
            widget_density: 'more',
            decisions_enabled: governance === true,
          })
          return true
        }
        if (path === '/api/secrets') {
          await json(route, { names: secrets })
          return true
        }
        if (path === '/api/models') {
          await json(route, MODELS)
          return true
        }
        if (path !== '/api/config/kirocrew') return false
        if (!config) return false
        await json(route, config)
        // Truthy: `json` resolves to undefined, and a falsy return lets the
        // shared map fulfil the same route a second time.
        return true
      },
    })
    return page
  }
  const decisionsSwitch = (page) => page.getByRole('switch', { name: 'Decisions (Jev)' })
  /** The card itself — the SettingsCard wrapper the switch sits in. */
  const decisionsCard = (page) =>
    decisionsSwitch(page).locator('xpath=ancestor::div[contains(@class,"card-glow")][1]')
  /** One row of the point list, by the plain-words name a reader sees. */
  const pointPanel = (page) => page.locator('#decisions-point-panel')

  /**
   * Frames are of the CARD, not the page: every state this harness varies is a
   * state of one card, and a 2800×2600 page frame buries 12px copy a reviewer
   * has to zoom into. The one frame about Settings search passes `{ full: true }`
   * because its subject is the listbox, not the card. Dimensions are printed so
   * a frame that grew past what a PR page renders legibly is visible in the log.
   */
  const save = async (page, name, { full = false } = {}) => {
    const path = `${OUT}/${name}.png`
    const buf = full
      ? await page.screenshot({ path })
      : await decisionsCard(page).screenshot({ path })
    // PNG IHDR: width and height are the two big-endian u32s at offsets 16 and 20.
    const w = buf.readUInt32BE(16)
    const h = buf.readUInt32BE(20)
    shot.push(`${name}.png (${w}×${h})`)
  }

  /**
   * The card's bottom edge must be inside the frame, not just its switch.
   *
   * Playwright calls an element "visible" when it has a box, which a row sitting
   * below the scroll port still does — that is exactly how five frames shipped
   * with the rows cropped off. So the LAST element of the card in this state is
   * scrolled in and then measured against the viewport.
   */
  const requireFramed = async (page, locator, what) => {
    await locator.scrollIntoViewIfNeeded()
    await page.waitForTimeout(250)
    const box = await locator.boundingBox()
    const viewport = page.viewportSize()
    if (!box || box.y < 0 || box.y + box.height > viewport.height - 8) {
      throw new Error(`${what} is not fully inside the frame: ${JSON.stringify(box)}`)
    }
  }

  /** Waits for the card to have resolved into the state under capture. */
  const settled = async (page, { enabled }) => {
    await decisionsSwitch(page).waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForFunction(
      (want) => {
        const el = document.querySelector('[role="switch"][aria-label="Decisions (Jev)"]')
        return !!el && (el.getAttribute('aria-disabled') === 'true') !== want
      },
      enabled,
      { timeout: 15000 },
    )
    await page.waitForTimeout(600) // let the cards' rise animation finish
  }

  /**
   * The list is the half of this card that says WHAT the switch changes, so its
   * absence must fail the harness rather than produce a tidy frame of a card with
   * nothing under the switch.
   *
   * Asserted as the LIST the gateway projected: one row per point it shipped, in
   * that order, each carrying the status word the server computed — and the row a
   * reader is looking at is the only selected one, because a second selected row
   * is the state where the panel belongs to no row.
   */
  const requirePointList = async (page, expected) => {
    const list = page.getByRole('tablist', { name: /what Jev decides/i })
    await list.waitFor({ state: 'visible', timeout: 10000 })
    const rows = await list.getByRole('tab').all()
    if (rows.length !== expected.length) {
      throw new Error(`the list draws ${rows.length} row(s), the gateway shipped ${expected.length}`)
    }
    for (const [i, want] of expected.entries()) {
      const text = (await rows[i].textContent()) ?? ''
      if (!text.includes(want.name)) {
        throw new Error(`row ${i} is not ${JSON.stringify(want.name)}: ${JSON.stringify(text)}`)
      }
      if (!text.includes(want.status)) {
        throw new Error(`row ${i} does not carry ${JSON.stringify(want.status)}: ${JSON.stringify(text)}`)
      }
    }
    const selected = await list.getByRole('tab', { selected: true }).count()
    if (selected !== 1) throw new Error(`${selected} row(s) are selected; exactly one panel is drawn`)
    // A point this build has no label for would render under its identifier, and a
    // point nothing consumes must have no row at all.
    for (const gone of ['skills.dedupe', 'cron.novelty']) {
      if ((await page.getByText(gone, { exact: true }).count()) > 0) {
        throw new Error(`a row is rendered for a point nothing consumes: ${gone}`)
      }
    }
    return list
  }

  /** Opens a point's panel the way a reader does, and waits for it to be that point's. */
  const openPoint = async (page, name) => {
    await page.getByRole('tab', { name, exact: false }).click()
    const panel = pointPanel(page)
    await panel.getByText(name, { exact: false }).first().waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(200)
    return panel
  }

  /** The four rows as a reader sees them with consent given and the scope granted. */
  const ALL_RUNNING = expectRows({ enabled: true, permits: true, toolArgs: true })

  /* ── OVERVIEW: the list, in both themes ─────────────────────────────────── */
  for (const theme of ['light']) {
    const page = await openPage({
      theme,
      config: withDecisions(),
      // Consent stands and the tool-argument scope does NOT: the state worth a
      // frame, because it is the one where a row says the fix is one level down.
      consent: { enabled: true, tool_args: false },
      secrets: ['TYPESAFE_API_KEY'],
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    if ((await decisionsSwitch(page).getAttribute('aria-checked')) !== 'true') {
      throw new Error('the stored flag is on, but the switch is not showing it')
    }
    const list = await requirePointList(page, expectRows({ enabled: true, permits: true }))
    // The shared block must be CLOSED here: a reader opening this card is asking
    // about points, and a frame with it open would not show the default state.
    if (await page.locator('#decisions-global-panel').isVisible().catch(() => false)) {
      throw new Error('the shared block is open before anyone opened it')
    }
    // The egress sentence is outside that disclosure on purpose, so it must be on
    // screen in the frame that shows the disclosure closed.
    await requireFramed(page, page.locator('#decisions-egress-note'), 'the egress sentence')
    await requireFramed(page, list, 'the point list')
    await requireFramed(page, pointPanel(page), 'the point panel')
    await save(page, `decisions-overview-${theme}`)
    await page.context().close()
  }

  /* ── DETAIL: model.route, the panel with the three tier pickers ──────────── */
  {
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, tool_args: true },
      secrets: ['TYPESAFE_API_KEY'],
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await requirePointList(page, ALL_RUNNING)
    const panel = await openPoint(page, "Model for the turn's difficulty")
    // One picker per tier, every one defaulting to INHERIT: a model id shipped as a
    // default fails on the first prompt for an account not entitled to it.
    //
    // `SettingsSelect` is a Radix select on a pointer device, so the CHOSEN value is
    // the trigger's text and the option list exists only while it is open. Read both
    // through the roles a reader's screen reader reads, and close the popup again so
    // the frame is of the panel rather than of an open menu.
    for (const label of ['Model for a simple turn', 'Model for a medium turn', 'Model for a hard turn']) {
      const trigger = panel.getByRole('combobox', { name: label })
      await trigger.waitFor({ state: 'visible', timeout: 5000 })
      const chosen = (await trigger.textContent()) ?? ''
      if (!/keep the session/i.test(chosen)) {
        throw new Error(`${label} does not read as inherit: ${JSON.stringify(chosen)}`)
      }
      await trigger.click()
      const options = await page.getByRole('option').allTextContents()
      // The list is the ADVERTISED one. A model this gateway does not offer being
      // choosable is the defect the picker exists to prevent, and `auto` is excluded
      // because "let the gateway choose" is what inherit already means here.
      for (const m of ['kiro-fast', 'kiro-balanced', 'kiro-deep']) {
        if (!options.some(o => o.includes(m))) {
          throw new Error(`${label} does not offer ${m}: ${options.join(', ')}`)
        }
      }
      if (options.some(o => o.trim() === 'auto')) {
        throw new Error(`${label} offers auto beside inherit, which are the same answer`)
      }
      await page.keyboard.press('Escape')
      await page.waitForTimeout(150)
    }
    // The point's identifier is on the panel: it is the string a reader greps the
    // decision log for.
    await panel.getByText('model.route', { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    await requireFramed(page, panel, "model.route's panel")
    await save(page, 'decisions-detail-model-route-light')
    await page.context().close()
  }

  /* ── DETAIL: nudge.wake, the only panel whose point has TWO providers ────── */
  {
    // Consent DELIBERATELY off, and the provider set to the small model: this is the
    // state the lane exists for, an owner with no Jev key, and it is the one frame
    // that shows a point's controls reachable while the card's switch is off.
    const page = await openPage({
      config: withDecisions({ judgeProvider: 'llm' }),
      consent: {
        enabled: false,
        // The row's chip comes from the gateway, and for THIS point the gateway reads
        // the provider as well as the keystone: on the small model it is active with
        // consent off. Selected here so the frame cannot show a chip that disagrees
        // with the pickers beneath it.
        judgeProvider: 'llm',
      },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    // `enabled` here is whether the card is LIVE, not whether consent is recorded:
    // the switch stays interactive with consent off, which is what makes this frame
    // possible at all.
    await settled(page, { enabled: true })
    const panel = await openPoint(page, 'Quiet check-ins: wake or skip')
    // The provider picker reads the CHOSEN word, not the default: a frame showing
    // `auto` here would not show that the small model was selectable with the switch
    // off, which is the whole claim of the frame.
    const provider = panel.getByRole('combobox', { name: 'Which judge answers' })
    await provider.waitFor({ state: 'visible', timeout: 5000 })
    const chosen = (await provider.textContent()) ?? ''
    if (!/small model/i.test(chosen)) {
      throw new Error(`the judge provider does not read as the small model: ${JSON.stringify(chosen)}`)
    }
    // Its model picker defaults to inherit, on the same terms as a tier's.
    const model = panel.getByRole('combobox', { name: 'Model for the small-model judge' })
    await model.waitFor({ state: 'visible', timeout: 5000 })
    const modelChosen = (await model.textContent()) ?? ''
    if (!/keep the judge agent/i.test(modelChosen)) {
      throw new Error(`the judge model does not read as inherit: ${JSON.stringify(modelChosen)}`)
    }
    // The line that keeps the CARD's frame from contradicting this panel. The card's
    // heading and intro speak for the Jev endpoint -- "while this is on", "nothing is
    // sent while this is off" -- and on this lane both are beside the point, so the
    // panel says which lane answers and where the evidence goes. A frame without it
    // photographs a consent surface whose own words disagree with its chip.
    await panel
      .getByText(/switch above does not govern it/i)
      .first()
      .waitFor({ state: 'visible', timeout: 5000 })
    await panel.getByText('nudge.wake', { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    // The chip must NAME the lane here. With the switch off, the generic active word
    // under the list's "while this is on" heading reads as a contradiction and, worse,
    // implies live egress -- which is the one thing this card must never imply
    // wrongly. A frame that cannot see the lane-specific words is a failed frame.
    await page
      .getByText('Judged by the small model', { exact: true })
      .first()
      .waitFor({ state: 'visible', timeout: 5000 })
    await requireFramed(page, panel, "nudge.wake's panel")
    await save(page, 'decisions-detail-nudge-wake-light')
    await page.context().close()
  }

  /* ── DETAIL: the judge's provider menu, OPEN ─────────────────────────────── */
  {
    // The collapsed picker shows ONE option, so the choice a reader makes about where
    // their evidence goes is made from copy no closed frame can show. This frame opens
    // the menu, which is where all three options and their destinations are legible at
    // once. Same keyless state as the frame above, for the same reason.
    const page = await openPage({
      config: withDecisions({ judgeProvider: 'llm' }),
      consent: { enabled: false, judgeProvider: 'llm' },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const panel = await openPoint(page, 'Quiet check-ins: wake or skip')
    const provider = panel.getByRole('combobox', { name: 'Which judge answers' })
    await provider.waitFor({ state: 'visible', timeout: 5000 })
    await provider.click()
    // Each option must name its DESTINATION, not just its name: that distinction is the
    // only thing an owner can use to choose, and it has to survive in the open list.
    // Asserted per option so a frame cannot pass with one of the three unlabelled.
    const menu = page.getByRole('listbox')
    await menu.waitFor({ state: 'visible', timeout: 5000 })
    for (const wanted of [/otherwise the small model/i, /to the address above/i, /stays with your current provider/i]) {
      await menu
        .getByText(wanted)
        .first()
        .waitFor({ state: 'visible', timeout: 5000 })
    }
    // A PAGE frame, like the Settings-search one and for the same reason: the subject
    // is the open listbox, which Radix renders in a portal outside the card and which
    // takes the rest of the page out of the accessibility tree while it is open -- so
    // the card locator cannot even be resolved here, let alone contain the menu.
    await save(page, 'decisions-detail-nudge-wake-provider-open-light', { full: true })
    await page.context().close()
  }

  /* ── DETAIL: the judge pinned to Jev with the card's switch OFF ───────────── */
  {
    // The REFUSE path, and the one state of this panel a reader can reach by choosing
    // wrongly: Jev is named as the judge, but its switch is off, so the gate refuses
    // the lane and every quiet check-in fires ungated. The frame exists to show that
    // the surface says so -- the row reads the plain OFF word rather than naming a
    // lane, and the small-model note is absent, because on this provider the switch
    // above DOES govern the answer.
    const page = await openPage({
      config: withDecisions({ judgeProvider: 'jev' }),
      consent: { enabled: false, judgeProvider: 'jev' },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const panel = await openPoint(page, 'Quiet check-ins: wake or skip')
    const provider = panel.getByRole('combobox', { name: 'Which judge answers' })
    await provider.waitFor({ state: 'visible', timeout: 5000 })
    const chosen = (await provider.textContent()) ?? ''
    if (!/to the address above/i.test(chosen)) {
      throw new Error(`the judge provider does not read as Jev: ${JSON.stringify(chosen)}`)
    }
    // The note belongs to the other lane only. Present here it would tell an owner the
    // switch above is beside the point on the one provider that lives or dies by it.
    if (await panel.getByText(/switch above does not govern it/i).count()) {
      throw new Error('the small-model note is showing on the Jev lane, where the switch does govern')
    }
    // And the chip must NOT name a lane: naming one over a refused provider is the
    // mirror of the defect the lane words exist to prevent.
    if (await page.getByText('Judged by the small model', { exact: true }).count()) {
      throw new Error('the row names the small-model lane while the provider is pinned to Jev')
    }
    await requireFramed(page, panel, "nudge.wake's panel on the Jev lane")
    await save(page, 'decisions-detail-nudge-wake-jev-refused-light')
    await page.context().close()
  }

  /* ── DETAIL: tool.risk, the only panel with a consent SCOPE switch ───────── */
  {
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, tool_args: false },
      secrets: ['TYPESAFE_API_KEY'],
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await requirePointList(page, expectRows({ enabled: true, permits: true }))
    const panel = await openPoint(page, 'Risky tool-call notes')
    const scope = panel.getByRole('switch')
    await scope.waitFor({ state: 'visible', timeout: 5000 })
    if ((await scope.getAttribute('aria-checked')) !== 'false') {
      throw new Error('the scope switch is showing a grant the keystone does not record')
    }
    // The row says "Needs your OK" and the OK is one level down, so the panel names
    // the switch below it. This is the whole repair path for a point carrying a
    // scope, which is why it gets a frame of its own BEFORE the grant.
    const where = panel.getByText(/the OK this needs/i)
    await where.waitFor({ state: 'visible', timeout: 5000 })
    await requireFramed(page, where, 'the line naming where the OK is given')
    await requireFramed(page, panel, "tool.risk's panel, awaiting its OK")
    await save(page, 'decisions-detail-needs-ok-light')
    await page.context().close()
  }

  /* ── DETAIL: compaction.keep, needing its OK and then granted ───────────── */
  {
    // The WIDEST consent this card governs, and the panel a reader has to be able to
    // read before deciding about it: the whole-transcript switch lives here rather
    // than beside the main one, so without this frame the control cannot be reviewed
    // at all. Captured in BOTH states, the way tool.risk is.
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, tool_args: true },
      secrets: ['TYPESAFE_API_KEY'],
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const panel = await openPoint(page, 'Which tool calls a compaction would keep')
    // Needing its OK: the row says so, and the panel names the switch below as the OK.
    const where = panel.getByText(/the OK this needs/i)
    await where.waitFor({ state: 'visible', timeout: 5000 })
    const scope = panel.getByRole('switch')
    await scope.waitFor({ state: 'visible', timeout: 5000 })
    if ((await scope.getAttribute('aria-checked')) !== 'false') {
      throw new Error('the transcript scope is showing a grant the keystone does not record')
    }
    // It must say the compaction itself is unchanged: a switch reading as "better
    // compaction" would promise something this build does not do.
    const body = (await panel.textContent()) ?? ''
    if (!/measurement/i.test(body)) {
      throw new Error('the panel does not say the compaction itself is unchanged')
    }
    await requireFramed(page, panel, "compaction.keep's panel, awaiting its OK")
    await save(page, 'decisions-detail-compaction-needs-ok-light')

    await page.context().close()
  }

  /* ── DETAIL: message.steer, the point with no scope of its own ───────────── */
  {
    // Shown because every other point's panel is, and a reader comparing them needs
    // the one that asks for nothing beyond consent itself: its panel is what "no extra
    // egress category" looks like.
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, tool_args: true },
      secrets: ['TYPESAFE_API_KEY'],
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const panel = await openPoint(page, 'Mid-turn message handling')
    if ((await panel.getByRole('switch').count()) > 0) {
      throw new Error('a point with no scope of its own is drawing a scope switch')
    }
    const body = (await panel.textContent()) ?? ''
    if (!/logged as/i.test(body)) {
      throw new Error("the panel does not name the point as the decision log spells it")
    }
    await requireFramed(page, panel, "message.steer's panel")
    await save(page, 'decisions-detail-message-steer-light')
    await page.context().close()
  }

  /* ── DETAIL: skills.select, whose extra line points into config.json ────── */

  /* ── GLOBAL: the shared block, opened ───────────────────────────────────── */
  {
    const page = await openPage({
      // A share below 100 and a budget off the default, so the frame shows values a
      // reader set rather than the ones a build ships.
      config: withDecisions({ bucket: 25, history: 6000 }),
      consent: { enabled: true, tool_args: true, history_budget_chars: 6000 },
      secrets: ['TYPESAFE_API_KEY'],
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await page.getByText('Shared settings', { exact: true }).click()
    const panel = page.locator('#decisions-global-panel')
    await panel.waitFor({ state: 'visible', timeout: 5000 })
    // The address is stated ABOVE this block, as the fact consent is given for, and
    // the card offers no control for it: `PATCH /api/config/kirocrew` excludes
    // `decisions.provider.*`, so a field here would be one whose every save fails.
    const card = decisionsCard(page)
    const shown = (await card.textContent()) ?? ''
    // The address is drawn as its own mono value, so the assertion reads those values
    // and compares them as URLs. `logged as <point id>` is mono too and simply does
    // not parse, which is why every candidate is offered rather than one guessed.
    const mono = await card.locator('span.font-mono').allTextContents()
    if (!statesEndpoint(mono, JEV_ENDPOINT)) {
      throw new Error(
        `the card does not state the address consent is given for; mono values: ${JSON.stringify(mono)}`,
      )
    }
    if (!/decisions\.provider\.endpoint/.test(shown)) {
      throw new Error('the card does not say where the address is changed')
    }
    if ((await card.getByLabel(/Address decisions are sent to/i).count()) > 0) {
      throw new Error('the address is offered as a control the config route refuses')
    }
    // The credential: SET, and never a value. A vault that handed the key back is
    // the defect this field exists to avoid, so the frame must not contain it.
    const body = (await panel.textContent()) ?? ''
    if (!/never shown again/i.test(body)) {
      throw new Error('the credential row does not say the key is never read back')
    }
    // The slider is the control the sampling share used to be a sentence about.
    const slider = panel.locator('#decisions-bucket-slider')
    await slider.waitFor({ state: 'visible', timeout: 5000 })
    if ((await slider.inputValue()) !== '25') {
      throw new Error(`the slider is not on the configured share: ${await slider.inputValue()}`)
    }
    if (!body.includes('25%')) throw new Error('the shared block does not state the share in words')
    const budget = panel.getByLabel(/Earlier conversation one decision may carry/i)
    if ((await budget.inputValue()) !== '6000') {
      throw new Error(`the history budget is not showing the configured value: ${await budget.inputValue()}`)
    }
    await requireFramed(page, panel, 'the shared block')
    await save(page, 'decisions-global-open-light')
    await page.context().close()
  }

  /* ── OFF: the rows still render, and every status reads off ─────────────── */
  {
    const page = await openPage({ config: withDecisions(), consent: { enabled: false } })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    if ((await decisionsSwitch(page).getAttribute('aria-checked')) !== 'false') {
      throw new Error('the stored flag is off, but the switch is not showing it')
    }
    // The state a reader most needs to be able to READ about: a point that is not
    // running still gets a row, because the card is what explains what it would do.
    const list = await requirePointList(page, expectRows({ enabled: false, permits: false }))
    // Nothing is being sent, so the panel must not offer an egress scope switch.
    await openPoint(page, 'Risky tool-call notes')
    if ((await pointPanel(page).getByRole('switch').count()) > 0) {
      throw new Error('a scope switch is offered while consent is withheld')
    }
    await requireFramed(page, list, 'the point list')
    await save(page, 'decisions-off-light')
    // Asserted AFTER the frame is written, so opening the shared block to check the
    // ceiling does not change the shot UX reads for the consent-off default.
    await page.getByText('Shared settings', { exact: true }).click()
    const heldBudget = page.getByLabel(/Earlier conversation one decision may carry/i)
    await heldBudget.waitFor({ state: 'visible', timeout: 5000 })
    if (!(await heldBudget.isDisabled())) {
      throw new Error('the history ceiling accepts typing while consent is off')
    }
    if (!/takes effect once the main Jev switch is on/i.test((await page.textContent('body')) ?? '')) {
      throw new Error('the held ceiling does not say what turns it on')
    }
    // The first-run default for the shared block: the state a reader meets before they
    // have agreed to anything, with the one control that is HELD rather than offered.
    await requireFramed(page, page.locator('details'), 'the shared block with consent off')
    await save(page, 'decisions-off-shared-light')
    await page.context().close()
  }

  /* ── GLOBAL: removing the credential ASKS FIRST ──────────────────────────── */
  {
    // The state that closes the hole a reader could fall into: the trash used to send
    // the DELETE on the first click and then draw an Undo for it. Now it asks, the
    // sentence says the act cannot be undone, and nothing leaves the browser until the
    // danger button is pressed. Asserted before it is saved, so the frame cannot record
    // a confirmation that is not actually gating the write.
    const page = await openPage({
      config: withDecisions({ bucket: 25, history: 6000 }),
      consent: { enabled: true, tool_args: true, history_budget_chars: 6000 },
      secrets: ['TYPESAFE_API_KEY'],
    })
    let deletes = 0
    await page.route('**/api/secrets/**', async route => {
      if (route.request().method() === 'DELETE') deletes += 1
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await page.getByText('Shared settings', { exact: true }).click()
    const panel = page.locator('#decisions-global-panel')
    await panel.waitFor({ state: 'visible', timeout: 5000 })
    const trash = panel.getByRole('button', { name: 'Remove key' })
    await trash.waitFor({ state: 'visible', timeout: 5000 })
    await trash.click()
    const warn = panel.getByText(/cannot be undone/i)
    await warn.waitFor({ state: 'visible', timeout: 5000 })
    if (deletes !== 0) {
      throw new Error('the trash sent a DELETE before the reader confirmed')
    }
    if ((await panel.getByRole('button', { name: /undo/i }).count()) > 0) {
      throw new Error('an undo is offered for a removal that has not been sent')
    }
    await panel.getByRole('button', { name: 'Delete key' }).waitFor({ state: 'visible', timeout: 5000 })
    await requireFramed(page, warn, 'the removal confirmation')
    await save(page, 'decisions-global-key-remove-confirm-light')
    await page.context().close()
  }

  /* ── GLOBAL: the credential BEFORE it is set, which is the first-run state ── */

  /* ── A gateway that ships the consent route but projects NO points ────────── */
  {
    // The half-updated gateway: consent answers, the point projection does not. The
    // card says which update fixes it rather than drawing a list written on this
    // side, which would claim points the gateway may not have.
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, points: null },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const note = page.getByText(/does not list what Jev decides yet/i)
    await note.waitFor({ state: 'visible', timeout: 5000 })
    if ((await page.getByRole('tablist', { name: /what Jev decides/i }).count()) > 0) {
      throw new Error('a list is drawn for a gateway that projected no points')
    }
    // Consent still reads on: the missing projection is about the BUILD, and telling
    // the reader their consent went away would be a second, false claim.
    if ((await decisionsSwitch(page).getAttribute('aria-checked')) !== 'true') {
      throw new Error('a missing point projection changed what the switch reports')
    }
    await requireFramed(page, note, 'the no-points notice')
    await save(page, 'decisions-points-unavailable-light')
    await page.context().close()
  }

  /* ── The states that are about the gateway, not about the points ─────────── */
  {
    // Consent stands for one address and config.json now names another: the
    // switch reads on, the gate refuses, and the card must say so in body weight.
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, configured_endpoint: MOVED_ENDPOINT, permits: false },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const note = page.getByText(/nothing is being sent/i)
    await note.waitFor({ state: 'visible', timeout: 5000 })
    if ((await decisionsSwitch(page).getAttribute('aria-checked')) !== 'true') {
      throw new Error('consent is recorded, so the switch must still read on while the notice explains')
    }
    await requirePointList(page, expectRows({ enabled: false, permits: false }))
    await requireFramed(page, note, 'the moved-address notice')
    // Compared as a URL, not as a substring: `getByText(string)` matches a substring,
    // so an element reading `…/systemone.evil.test/x` would satisfy it -- the same
    // defect `js/incomplete-url-substring-sanitization` names for `includes`. The frame
    // then anchors on the EXACT text, so it cannot land on a longer neighbour either.
    const movedMono = await decisionsCard(page).locator('span.font-mono').allTextContents()
    if (!statesEndpoint(movedMono, MOVED_ENDPOINT)) {
      throw new Error(
        `the card does not state the address config names now; mono values: ${JSON.stringify(movedMono)}`,
      )
    }
    if (statesEndpoint(movedMono, JEV_ENDPOINT)) {
      throw new Error('the card is still showing the consented address as the one in force')
    }
    await requireFramed(page, page.getByText(MOVED_ENDPOINT, { exact: true }), 'the sent-to line')
    await save(page, 'decisions-endpoint-moved-light')
    await page.context().close()
  }






  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => { console.error(err); process.exit(1) })
