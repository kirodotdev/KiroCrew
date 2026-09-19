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
 * The card's whole point is that its switch is backend state rather than a
 * localStorage preview flag — the KEYSTONE `decisions_consent.json` behind
 * `/api/decisions/consent`, with the sampling share from `config.json` — so its
 * states are states of those two answers, which is exactly what this harness can
 * vary. Ten frames:
 *   decisions-off-{light,dark}.png   a gateway whose keystone says `enabled: false`,
 *                                    with the one live point named read-only
 *   decisions-on-{light,dark}.png    the same gateway with consent given
 *   decisions-sampled-light.png      on, with a bucket that narrows which sessions
 *                                    the point decides for; the line names the
 *                                    config path that sets it
 *   decisions-endpoint-moved-light.png
 *                                    consent given for one address, config now naming
 *                                    another: on, but nothing is sent, and it says so
 *   decisions-legacy-preview-light.png
 *                                    a shadow-era gateway: no consent route (404),
 *                                    config carrying `decisions.preview` — unsupported,
 *                                    because that flag was consent to a
 *                                    measurement, not to acting on the answer, and
 *                                    nothing in config.json is read as consent
 *   decisions-backend-missing-light.png
 *                                    a gateway with no consent route and no
 *                                    `decisions` section at all: same disabled
 *                                    switch, same reason
 *   decisions-read-failed-light.png  the consent read itself failed (503) — a
 *                                    different note, because the fix is a retry
 *                                    not an update
 *   decisions-save-failed-light.png  the write was refused: the switch is back on
 *                                    the stored value and says so
 *   decisions-search-light.png       Settings search reaching the toggle
 *
 * WHAT THIS HARNESS ASSERTS, and why it asserts anything at all: a capture script
 * that only writes PNGs fails toward a false pass — a fixture typo, a clipped
 * card or a state that never arrived all still produce a tidy image a PR can cite.
 * The first version of this file guarded only that the SWITCH was inside the
 * viewport, and the UX review lane then reported that every frame cropped the
 * check rows off the bottom. So the guard is on the card's LAST element in each
 * state, and each state additionally asserts the text that makes it that state.
 *
 * Usage: node scripts/capture-decisions-preview-card.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decisions-preview-card'
mkdirSync(OUT, { recursive: true })

/**
 * A gateway that carries the field this switch writes.
 *
 * `bucket` defaults to 100 because that is what a real gateway returns for an
 * untouched config, and the card prints that share ("all of your sessions") in
 * both switch states -- the off/on frames must show the default a reader consents to.
 * Consent itself is NOT in this config: it is the keystone, answered by the
 * `consent` option of `openPage`.
 */
/** The consent GET/PUT payload the gateway returns, bound to the default endpoint. */
const JEV_ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
const consentPayload = ({ enabled = false, configured_endpoint = JEV_ENDPOINT, permits } = {}) => ({
  enabled,
  endpoint: enabled ? JEV_ENDPOINT : '',
  configured_endpoint,
  permits: permits ?? (enabled && configured_endpoint === JEV_ENDPOINT),
})

const withDecisions = (bucket = 100) => ({
  ...KIROCREW_CONFIG_FIXTURE,
  decisions: { bucket, provider: { model: 'jev-latest' } },
})

/** A shadow-era gateway: the section exists, the field this switch writes does not. */
const LEGACY_PREVIEW_CONFIG = {
  ...KIROCREW_CONFIG_FIXTURE,
  decisions: {
    preview: true,
    points: { 'skills.select': { arm: 'shadow', impl: 'llm', bucket: 100 } },
  },
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []

  /**
   * `consent` is the keystone's answer: an object for a supported gateway, or
   * an HTTP status (404 = older gateway with no consent route, 503 = read failed).
   *
   * @param {{theme?: string, config?: object|null, consent?: object|number,
   *          putStatus?: number}} opts
   */
  const openPage = async ({ theme = 'light', config = null, consent = 404, putStatus = 200 } = {}) => {
    const context = await browser.newContext({
      // Tall enough for the whole section plus the card's rows. The assertions
      // below are what actually hold the framing; this is only a starting size.
      // The width is chosen so the card frame at 2× stays under 1800px wide,
      // which is what a PR page renders without downscaling.
      viewport: { width: 1360, height: 1300 },
      deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
    })
    const page = await context.newPage()
    logPageProblems(page)
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
              await json(route, consentPayload({ enabled: sent.enabled === true }))
            }
            return true
          }
          if (typeof consent === 'number') {
            await json(route, { error: consent === 404 ? 'not found' : 'keystone unreadable' }, consent)
          } else {
            await json(route, consentPayload(consent))
          }
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
   * with the check rows cropped off. So the LAST element of the card in this
   * state is scrolled in and then measured against the viewport.
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
   * The point row is the half of this card that says WHAT the switch changes, so
   * its absence must fail the harness rather than produce a tidy frame of a card
   * with nothing under the switch. Asserted as the pair a reader sees — the
   * plain-words gloss and the mono identifier — and as NOT carrying an on/off
   * word of its own: the switch is the card's one state, and a second one is the
   * duplicate the UX review read as two controls.
   */
  const requirePointRow = async (page) => {
    const point = page.getByText('skills.select', { exact: true })
    await point.waitFor({ state: 'visible', timeout: 5000 })
    const gloss = page.getByText('Automatic skill choice', { exact: true })
    await gloss.waitFor({ state: 'visible', timeout: 5000 })
    await page.getByText(/what Jev decides while this is on/i).waitFor({ state: 'visible', timeout: 5000 })
    // Gloss, then the identifier introduced as the log's name -- and nothing else.
    const row = await point.evaluate(el => el.parentElement?.parentElement?.textContent ?? '')
    if (row !== 'Automatic skill choicelogged as skills.select') {
      throw new Error(`the point row carries more than the gloss and the identifier: ${JSON.stringify(row)}`)
    }
    // Only the one consumed point has a row.
    for (const gone of ['skills.dedupe', 'cron.novelty']) {
      if (await page.getByText(gone, { exact: true }).count() > 0) {
        throw new Error(`a row is rendered for a point nothing consumes: ${gone}`)
      }
    }
    return point
  }

  for (const theme of ['light', 'dark']) {
    const page = await openPage({ theme, config: withDecisions(), consent: { enabled: false } })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    if (await decisionsSwitch(page).getAttribute('aria-checked') !== 'false') {
      throw new Error('the stored flag is off, but the switch is not showing it')
    }
    const point = await requirePointRow(page)
    // Off still states the share, as a fact about the on state: the default is
    // every session, and a reader consents to that BEFORE flipping the switch.
    const share = page.getByText(/While this is on, Jev answers for all of your sessions/i)
    if (await share.count() !== 1) {
      throw new Error('the off card does not state the default share (all sessions)')
    }
    await requireFramed(page, share, 'the share line')
    await requireFramed(page, point, 'the point row')
    await requireFramed(page, decisionsSwitch(page), 'the Decisions switch')
    await save(page, `decisions-off-${theme}`)
    await page.context().close()
  }

  for (const theme of ['light', 'dark']) {
    const page = await openPage({ theme, config: withDecisions(), consent: { enabled: true } })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    if (await decisionsSwitch(page).getAttribute('aria-checked') !== 'true') {
      throw new Error('the stored flag is on, but the switch is not showing it')
    }
    const point = await requirePointRow(page)
    await requireFramed(page, point, 'the point row')
    await save(page, `decisions-on-${theme}`)
    await page.context().close()
  }

  {
    // On, and deciding for only a quarter of sessions — the state the sampling
    // line exists for. An operator moves this in `config.json`; the card has no
    // control for it on purpose.
    const page = await openPage({ config: withDecisions(25), consent: { enabled: true } })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await requirePointRow(page)
    // Matched on the sampling sentence's own opening: the card's description
    // also says "of your sessions", and a loose pattern hits both.
    const rate = page.getByText(/Jev answers for/i)
    await rate.waitFor({ state: 'visible', timeout: 5000 })
    const text = await rate.textContent()
    if (!text.includes('25%')) throw new Error(`the sampling line does not name the rate: ${text}`)
    // The card has no control for the rate, so the line must say where it is set.
    if (!text.includes('decisions.bucket')) throw new Error(`the sampling line does not name the knob: ${text}`)
    await requireFramed(page, rate, 'the sampling line')
    await save(page, 'decisions-sampled-light')
    await page.context().close()
  }

  {
    // Consent stands for one address and config.json now names another: the
    // switch reads on, the gate refuses, and the card must say so in body weight.
    const page = await openPage({
      config: withDecisions(),
      consent: { enabled: true, configured_endpoint: 'https://proxy.example/v1/systemone', permits: false },
    })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const note = page.getByText(/nothing is being sent/i)
    await note.waitFor({ state: 'visible', timeout: 5000 })
    if (await decisionsSwitch(page).getAttribute('aria-checked') !== 'true') {
      throw new Error('consent is recorded, so the switch must still read on while the notice explains')
    }
    await requireFramed(page, note, 'the moved-address notice')
    await requireFramed(page, page.getByText('https://proxy.example/v1/systemone'), 'the sent-to line')
    await save(page, 'decisions-endpoint-moved-light')
    await page.context().close()
  }

  {
    // A gateway from the shadow release: it HAS a `decisions` section, with the
    // retired `preview` flag set to true. Support keys off the `enabled` FIELD, so
    // this is an old gateway — and that old yes is not consent to acting on an
    // answer.
    const page = await openPage({ config: LEGACY_PREVIEW_CONFIG, consent: 404 })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: false })
    const note = page.getByText(/older than this feature/i)
    await note.waitFor({ state: 'visible', timeout: 5000 })
    if (await decisionsSwitch(page).getAttribute('aria-checked') !== 'false') {
      throw new Error('the retired preview flag is being shown as this switch being on')
    }
    await requireFramed(page, note, 'the backend-update note')
    await save(page, 'decisions-legacy-preview-light')
    await page.context().close()
  }

  {
    // No `decisions` section at all: the frontend ships before the backend field
    // whenever a user updates one half first, so the switch has to refuse rather
    // than offer a write that returns 400.
    const page = await openPage({ config: KIROCREW_CONFIG_FIXTURE, consent: 404 })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: false })
    const note = page.getByText(/older than this feature/i)
    await note.waitFor({ state: 'visible', timeout: 5000 })
    if (await page.getByText('skills.select', { exact: true }).count() > 0) {
      throw new Error('a point row is rendered against a gateway that has no point')
    }
    await requireFramed(page, note, 'the backend-update note')
    await save(page, 'decisions-backend-missing-light')
    await page.context().close()
  }

  {
    // A DIFFERENT state with a different fix: the config could not be read at
    // all, so the card must not blame the gateway's version for it.
    const page = await openPage({ config: withDecisions(), consent: 503 })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: false })
    const notice = page.getByText(/could not read the settings/i)
    await notice.waitFor({ state: 'visible', timeout: 10000 })
    await requireFramed(page, notice, 'the read-failed notice')
    await save(page, 'decisions-read-failed-light')
    await page.context().close()
  }

  {
    // The write refused: the switch shows the stored value, not the click's.
    const page = await openPage({ config: withDecisions(), consent: { enabled: false }, putStatus: 403 })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await decisionsSwitch(page).click()
    const notice = page.getByText(/could not save this setting/i)
    await notice.waitFor({ state: 'visible', timeout: 10000 })
    if (await decisionsSwitch(page).getAttribute('aria-checked') !== 'false') {
      throw new Error('the switch is showing the refused write as if it had taken')
    }
    await requireFramed(page, notice, 'the save-failed notice')
    await save(page, 'decisions-save-failed-light')
    await page.context().close()
  }

  {
    // The toggle carries no configKey (it writes a keystone, not a config path),
    // so the search hit reaches it by registry id + label — worth its own frame
    // to show that path still lands on the switch.
    const page = await openPage({ config: withDecisions(), consent: { enabled: false } })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const input = page.getByRole('combobox', { name: 'Search settings' })
    await input.fill('decisions')
    await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    await save(page, 'decisions-search-light', { full: true })
    await page.context().close()
  }

  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => { console.error(err); process.exit(1) })
