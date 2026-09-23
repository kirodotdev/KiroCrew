/**
 * Screenshot harness for the Crew page work-item board (RFC Phase 4, the surfaces).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static server
 * with SPA fallback, and answers GET /api/crew-board from a fixture.
 *
 * The two fixtures are not hand-written. They are the VERBATIM output of the real
 * handler — `dashboard/handlers/work_ledger_board.api_work_ledger_board` called
 * in-process over a seeded two-conductor work-ledger store, then dumped. That
 * matters for what these shots are evidence OF: a hand-written payload would agree
 * with the page by construction, so it could not show that the masked projection
 * and the page actually fit together. Neither fixture contains a `chat-*` worker
 * key or the string `worker_session_key`, which is the masking criterion holding.
 *
 * Two boards, because orphanhood is a property of the CONDUCTOR's session and the
 * two states show different affordances:
 *
 *   alive     — the conductor's slot is open. Nothing is orphaned, so no row offers
 *               an action. The ordinary board: decision band, working band, and
 *               finished items collapsed behind the expander.
 *   orphaned  — the conductor's slot is gone, so every open item is orphaned. This
 *               is the state Phase 4 names its affordances for. Stop is live on the
 *               item whose worker is still running and disabled with a reason on the
 *               one whose session has closed; Take over renders no control, because
 *               no server-side primitive exists to perform it.
 *
 * Five shots, not two. The board's ordinary states go in light AND dark, so the
 * theme-variable claim is checked rather than asserted — a hard-coded colour would
 * survive one and fail the other. The remaining three are states a reader cannot
 * reach from a board shot and so has to take on trust: the per-item event tail
 * opened, a failed action, and a session that keeps no ledger at all. They reuse
 * the same fixtures and differ only in what the harness does to the page, so
 * covering them costs no additional recorded payload.
 *
 * Usage: node scripts/capture-crew-board.mjs [outDir]
 *
 * Writes into `docs/request-for-change/assets/` by DEFAULT, and those files are
 * committed. Design and UX review both read rendered evidence from the revision
 * itself, so evidence that lives only in a gitignored scratch directory is evidence
 * no reviewer can see. Defaulting here means re-running the harness refreshes the
 * committed images rather than silently leaving them stale.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../docs/request-for-change/assets'
const PREFIX = 'crew-board-'
const FIXTURES = fileURLToPath(new URL('./fixtures/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const scene = (name) =>
  JSON.parse(readFileSync(`${FIXTURES}crew-board-${name}.json`, 'utf8'))

const SCENES = {
  alive: scene('alive'),
  orphaned: scene('orphaned'),
  empty: scene('empty'),
}

// The page's own English strings, so a wait matches whatever the catalog says.
const STRINGS = JSON.parse(
  readFileSync(fileURLToPath(new URL('../src/i18n/locales/en.manual.json', import.meta.url)), 'utf8')
).pages.crewBoard

/** The slot the menu shot's chat page runs on — the conductor whose board exists. */
const MENU_SLOT = SCENES.alive.conductor.slot_key

/** The shots, in the order a reader should meet them. */
const SHOTS = [
  { scene: 'alive', label: 'board', modes: ['light', 'dark'] },
  { scene: 'orphaned', label: 'orphaned-actions', modes: ['light', 'dark'] },
  // The entry point. Not reachable from the board itself, so it is shot where it
  // lives: the session header's overflow menu on a conducting session.
  { scene: 'alive', label: 'menu-entry', modes: ['dark'], openMenu: true },
  // A ledger that exists and holds nothing, which is a different answer from a
  // session that keeps no ledger at all.
  { scene: 'empty', label: 'empty-board', modes: ['dark'] },
  // The event tail is collapsed by default, so a board shot can only ever show the
  // closed control. This one opens it.
  { scene: 'orphaned', label: 'events-open', modes: ['dark'], openEvents: true },
  // A failed Stop. The route is answered 500 and the button clicked, so the notice
  // is the page's real error path rather than a mocked-up element.
  { scene: 'orphaned', label: 'action-failed', modes: ['dark'], failAction: true },
  // Most sessions keep no work ledger, so this is what a reader arriving from the
  // menu of an ordinary session would see -- if the menu offered it, which is
  // exactly why the entry hides itself.
  { scene: 'no-ledger', label: 'no-ledger', modes: ['dark'] },
  // 320px, the narrowest width the layout rule names. The row's two metadata
  // rails stack under the title here instead of squeezing it, and the event tail
  // wraps rather than holding its pinned columns.
  { scene: 'orphaned', label: 'narrow-320', modes: ['dark'], openEvents: true, viewport: { width: 320, height: 900 } },
]

async function shoot(page, base, shot) {
  const { scene: name, mode, openEvents, failAction, openMenu } = shot
  const payload = SCENES[name]
  // Each board is its OWN ledger, so the conductor comes from the payload rather
  // than from a module constant: the orphaned scene is a different, smaller one.
  // The no-ledger shot has no payload, so it names a session that owns nothing.
  const conductor = payload ? payload.conductor.slot_key : 'chat-0-no-ledger'

  await page.addInitScript((theme) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', theme)
    localStorage.setItem('mc-onboarded', '1')
  }, mode)

  // The shell opens a live socket on boot. Nothing here needs it, and left
  // unrouted it logs a handshake error on every run of this harness.
  await page.routeWebSocket(/\/api\/ws/, () => {})

  // Only the two crew-board routes are this harness's own; every other boot
  // endpoint comes from the shared stub that 279 capture scripts already use. Its
  // `extra` arm is consulted first, so these win over the default map.
  await stubDashboardApi(page, {
    theme: mode,
    // Only the menu shot needs a session to exist: it is the chat header that hosts
    // the entry, so the board routes alone cannot show it.
    ...(openMenu
      ? {
          slots: [
            {
              key: MENU_SLOT,
              messages: 4,
              running: false,
              agent: 'kirocrew-conductor',
              mode: '',
            },
          ],
        }
      : {}),
    extra: (path, route) => {
      if (path === '/api/crew-board') {
        if (!payload) {
          // What the route really answers for a session with no ledger, so the page
          // takes its own 404 branch instead of one staged for it.
          json(route, { error: 'no work ledger for this session', code: 'no_ledger' }, 404)
          return true
        }
        json(route, payload)
        return true
      }
      if (path === '/api/crew-board/action') {
        json(route, { error: 'the gateway could not stop this worker', code: 'stop_failed' }, 500)
        return true
      }
      return false
    },
  })

  if (openMenu) {
    // The entry lives in the session header's overflow menu, so this shot drives
    // the chat page rather than the board route.
    await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Session options' }).first().click()
    // The entry's own label, not a timeout: it appears only once the board probe
    // has answered, which is the behaviour this shot is evidence of.
    await page.getByText('Crew board').waitFor({ timeout: 20000 })
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${PREFIX}${shot.file}` })
    console.log('wrote', `${OUT}/${PREFIX}${shot.file}`)
    await page.unroute('**/api/**')
    return
  }

  await page.goto(`${base}/crew-board?conductor=${encodeURIComponent(conductor)}`, {
    waitUntil: 'domcontentloaded',
  })

  if (!payload) {
    // The empty-state heading, read from the catalog rather than retyped, so a
    // reworded string fails the i18n gate and not this harness twenty seconds in.
    await page.getByText(STRINGS.no_ledger_title, { exact: false }).waitFor({ timeout: 20000 })
  } else if (payload.items.length === 0) {
    // A ledger with nothing in it draws no bands at all, so there is no band
    // heading to wait on -- the empty state is the whole content.
    await page.getByText(STRINGS.empty_title, { exact: false }).waitFor({ timeout: 20000 })
  } else {
    // Wait on CONTENT from the fixture, not a timeout: if the board failed to
    // render the shot must fail here rather than quietly capturing an empty page.
    await page.getByText(STRINGS.band_ruling).first().waitFor({ timeout: 20000 })

    // Expand the finished band FIRST, so the terminal collapse is visible in the
    // shot and so the per-item wait below can see a terminal row at all -- it is
    // behind this expander, which is the whole point of the band.
    const expander = page.getByRole('button', { name: /Finished/i })
    if (await expander.count()) await expander.first().click()

    // Every title is read OUT of the payload, so the scenes need no hardcoded
    // strings and a re-recorded fixture cannot leave a stale assertion behind.
    for (const item of payload.items) {
      await page.getByText(item.title, { exact: false }).first().waitFor({ timeout: 20000 })
    }

    if (openEvents) {
      const events = page.getByRole('button', { name: /Events/i })
      const n = await events.count()
      if (!n) throw new Error('events-open: no Events control on the board')
      for (let i = 0; i < n; i++) await events.nth(i).click()
    }

    if (failAction) {
      const stop = page.getByRole('button', { name: STRINGS.action_stop }).first()
      await stop.click()
      // The notice, not a timeout: a shot taken before the request settles would
      // show the page mid-flight and prove nothing about the error path.
      await page.getByText(/did not go through/i).waitFor({ timeout: 20000 })
    }
  }

  // Back to the top of every scroller before the shot. Playwright scrolls a
  // control into view before clicking it, and this shell renders the page inside
  // its own scroll container under a fixed header -- so after an expander click a
  // narrow viewport sits mid-page, and `fullPage` cannot reach back above that
  // offset: the card title and the goal line are simply absent from the image.
  await page.evaluate(() => {
    window.scrollTo(0, 0)
    document.querySelectorAll('*').forEach(el => {
      if (el.scrollTop) el.scrollTop = 0
    })
  })

  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}${shot.file}`, fullPage: true })
  console.log('wrote', `${OUT}/${PREFIX}${shot.file}`)

  await page.unroute('**/api/**')
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  for (const shot of SHOTS) {
    for (const mode of shot.modes) {
      const context = await browser.newContext({
        viewport: shot.viewport ?? { width: 1280, height: 900 },
        deviceScaleFactor: 2,
        colorScheme: mode,
      })
      const page = await context.newPage()
      logPageProblems(page)
      await shoot(page, base, { ...shot, mode, file: `${shot.label}-${mode}.png` })
      await context.close()
    }
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
