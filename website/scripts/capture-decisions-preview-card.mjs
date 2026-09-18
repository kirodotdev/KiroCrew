/**
 * Screenshot harness for the Decisions (Jev) card in Settings > Developer >
 * Feature Previews.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * The card's whole point is that its switch is backend config rather than a
 * localStorage preview flag, so its states are states of the CONFIG — which is
 * exactly what this harness can vary. Seven frames:
 *   decisions-off-{light,dark}.png   a gateway that carries `decisions`, flag off,
 *                                    with the three checks and their modes
 *   decisions-on-light.png           the same gateway with the flag on
 *   decisions-backend-missing-light.png
 *                                    a gateway whose config has no `decisions`
 *                                    section: the switch disabled, with the reason
 *   decisions-read-failed-light.png  the config read itself failed — a different
 *                                    note, because the fix is a retry not an update
 *   decisions-save-failed-light.png  the write was refused: the switch is back on
 *                                    the stored value and says so
 *   decisions-search-light.png       Settings search reaching the new toggle
 *                                    through its registry entry
 *
 * WHAT THIS HARNESS ASSERTS, and why it asserts anything at all: a capture script
 * that only writes PNGs fails toward a false pass — a fixture typo, a clipped
 * card or a state that never arrived all still produce a tidy image a PR can cite.
 * The first version of this file guarded only that the SWITCH was inside the
 * viewport, and the UX review lane then reported that every frame cropped the
 * check rows off the bottom. So the guard is now on the card's LAST element in
 * each state, not on its first.
 *
 * Usage: node scripts/capture-decisions-preview-card.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/decisions-preview-card'
mkdirSync(OUT, { recursive: true })

/** A gateway that carries the section the backend PR adds. */
const withDecisions = (preview) => ({
  ...KIROCREW_CONFIG_FIXTURE,
  decisions: {
    preview,
    points: {
      'skills.select': { arm: 'shadow', impl: 'llm', bucket: 100 },
      'skills.dedupe': { arm: 'shadow', impl: 'llm', bucket: 100 },
      'cron.novelty': { arm: 'off', impl: 'llm', bucket: 100 },
    },
  },
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []

  /**
   * @param {{theme?: string, config?: object|null, configStatus?: number,
   *          patchStatus?: number}} opts
   */
  const openPage = async ({ theme = 'light', config = null, configStatus = 200, patchStatus = 200 } = {}) => {
    const context = await browser.newContext({
      // Tall enough for the whole section plus the card's rows. The assertions
      // below are what actually hold the framing; this is only a starting size.
      viewport: { width: 1400, height: 1300 },
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
        if (path !== '/api/config/kirocrew') return false
        if (route.request().method() === 'PATCH') {
          await json(route, { error: 'field not editable: decisions.preview' }, patchStatus)
          return true
        }
        if (configStatus !== 200) {
          await json(route, { error: 'config unreadable' }, configStatus)
          return true
        }
        if (!config) return false
        await json(route, config)
        // Truthy: `json` resolves to undefined, and a falsy return lets the
        // shared map fulfil the same route a second time.
        return true
      },
    })
    return page
  }
  const save = async (page, name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    shot.push(`${name}.png`)
  }

  const decisionsSwitch = (page) => page.getByRole('switch', { name: 'Decisions (Jev)' })

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

  for (const theme of ['light', 'dark']) {
    const page = await openPage({ theme, config: withDecisions(false) })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    // The rows are the half of this card the review lane could not evaluate, so
    // their absence must fail the harness rather than produce a tidy frame of a
    // card with nothing under the switch.
    for (const point of ['skills.select', 'skills.dedupe', 'cron.novelty']) {
      await page.getByText(point, { exact: true }).waitFor({ state: 'visible', timeout: 5000 })
    }
    await requireFramed(page, page.getByText('cron.novelty', { exact: true }), 'the last check row')
    await requireFramed(page, decisionsSwitch(page), 'the Decisions switch')
    await save(page, `decisions-off-${theme}`)
    await page.context().close()
  }

  {
    const page = await openPage({ config: withDecisions(true) })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    await requireFramed(page, page.getByText('cron.novelty', { exact: true }), 'the last check row')
    await save(page, 'decisions-on-light')
    await page.context().close()
  }

  {
    // The state on `main` today: the frontend ships before the backend field, so
    // the switch has to refuse rather than offer a write that returns 400.
    const page = await openPage({ config: KIROCREW_CONFIG_FIXTURE })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: false })
    const note = page.getByText(/older than this switch/i)
    await note.waitFor({ state: 'visible', timeout: 5000 })
    await requireFramed(page, note, 'the backend-update note')
    await save(page, 'decisions-backend-missing-light')
    await page.context().close()
  }

  {
    // A DIFFERENT state with a different fix: the config could not be read at
    // all, so the card must not blame the gateway's version for it.
    const page = await openPage({ configStatus: 503 })
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
    const page = await openPage({ config: withDecisions(false), patchStatus: 400 })
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
    // The registry entry is the only way this toggle is reachable by search, and
    // it carries no configKey — so the search hit is worth its own frame.
    const page = await openPage({ config: withDecisions(false) })
    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
    await settled(page, { enabled: true })
    const input = page.getByRole('combobox', { name: 'Search settings' })
    await input.fill('decisions')
    await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    await save(page, 'decisions-search-light')
    await page.context().close()
  }

  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => { console.error(err); process.exit(1) })
