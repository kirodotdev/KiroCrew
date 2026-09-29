/**
 * Screenshot harness for the trust-and-enable modal's FAILURE states and the
 * detail page's install-refusal error box.
 *
 * Runs the REAL built SPA behind the shared static server and answers every
 * /api/** call from fixtures — including the SSE install stream — so no gateway
 * and no kiro-cli is needed.
 *
 * Three states, one flow each, every one starting from a fresh page:
 *
 *   01 failure            → the modal after the retried install failed with the
 *                           install-time gate's PERMANENT refusal (its sentence
 *                           plus the machine code), the grant rolled back
 *   02 transient-failure  → the modal after the retried install failed with a
 *                           server reason the gateway did NOT mark permanent (a
 *                           build failure, no code): the reason is shown, Try
 *                           again stays
 *   03 trusted-page-banner → the detail page when the app is ALREADY trusted: no
 *                           consent modal opens, the first install attempt is
 *                           refused, and the page's own error box carries the
 *                           refusal
 *
 * BEFORE (`before` prefix, served from a dist built at the PR's base commit)
 * captures 01 only and asserts the modal shows ONLY the generic copy with the
 * server's sentence nowhere in it. AFTER captures all three: 01 asserts the
 * permanent-refusal rendering (the modal's headline naming the app in the
 * install words of the button that opened the flow, with no retry instruction,
 * the plain-language sentence naming the gateway remedy as an action, the
 * server's own sentence beneath, the footer offering Close only and no
 * sentence about what Cancel would do); 02 asserts the generic headline kept as
 * the title with the server's reason beneath it, Trust and try again (named for
 * the grant it re-writes) and Cancel still
 * offered and the Cancel sentence with them; 03 asserts no dialog, the page's error box
 * carrying the same install-framed headline and the plain sentence -- the
 * server's sentence NOT repeated inside it, since the install-log panel
 * beneath shows it verbatim -- with a dismiss control whose visible label
 * names it as the way back to Install, the Install button disabled beneath it
 * and titled with what re-enables it. Each assertion
 * runs before its frame is taken, so a screenshot can never show a state the
 * run did not verify.
 *
 * Usage: node scripts/capture-trust-modal-install-reason.mjs [outDir] [prefix] [dist]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/trust-modal-install-reason'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const APP = 'agent-dashboard'
const DISPLAY = 'Agent Dashboard'
const REPO = 'https://git.example.test/apps/agent-dashboard.git'

/** The install-time gate's own refusal — the sentence the modal renders beneath
 *  its plain-language copy. */
const REFUSAL =
  'Python apps that require a build step are not supported in the desktop app: ' +
  'its bundled interpreter is inside the signed application bundle and cannot ' +
  'install packages'
/** The code the gate returns beside it: a permanent condition for this gateway. */
const REFUSAL_CODE = 'desktop_build_step_unsupported'
/** A reason the gateway does NOT mark permanent: a build that may well pass next time. */
const TRANSIENT_REASON = 'build failed (exit 1): npm install: ETIMEDOUT registry.npmjs.org'

/** The generic copy: the whole message BEFORE, gone AFTER for a permanent refusal. */
/** The not-granted headline (`failed_generic`): the modal states the grant's actual
 *  state -- trust was not saved -- instead of a hedge. */
const GENERIC = 'could not be turned on, and trust was not saved'
/** The same headline as the BASE build words it (the BEFORE dist is built at the
 *  PR's base commit, whose catalog still reads "nothing was changed"). */
const GENERIC_BEFORE = 'could not be turned on, and nothing was changed'
/** The granted headline (`failed`): the grant landed and stands, the retry failed;
 *  both sentences open with the one clause tying trust and "turned on" together. */
const GRANTED = 'It is now trusted, but it could not be turned on'
const ONE_SWITCH = `Trusting \u201c${DISPLAY}\u201d is what turns it on.`
const REMOVE_TRUST = 'Trusted third-party apps to remove the trust'
const ENABLE_FAILED_REASON = 'backend failed to start: ModuleNotFoundError: fastapi'
/** The instruction the generic copy carries and a permanent refusal must not. */
/** The retry button, named for both things it does (it re-writes the trust grant
 * before retrying); the generic copy's instruction names it the same way. */
const RETRY_BUTTON = 'Trust and try again'
const RETRY_INSTRUCTION = `Choose ${RETRY_BUTTON}`
/** The AFTER copy in the modal: the verb of the action the user pressed to get here
 * (Install -- the build step this refusal names runs at install and nowhere else)
 * and the dialog's assurance, naming the app. */
const DESKTOP_HEADLINE = `\u201c${DISPLAY}\u201d can't be installed in the desktop version of Kiro Crew, and nothing was changed.`
/** The AFTER copy on the detail page: the same verb, so a reader who meets the
 * modal's sentence and then this banner behind it is never left wondering whether
 * installing and turning on are two steps. */
const PAGE_HEADLINE = "This app can't be installed in the desktop version of Kiro Crew."
/** The plain sentence both surfaces share, and the remedy it names as an action. */
const DESKTOP_HELP = "It needs Python packages the desktop build can't add"
const GATEWAY_ACTION = 'open Kiro Crew in a web browser instead of the desktop version'
/** The page's own trailing sentence, in the UX lane's words: what re-enables Install. */
const RETRY_PATH = 'Once the author ships the packages, dismiss this notice to install again.'
/** The banner's dismiss control names what dismissing does (it re-enables Install), as
 * visible text that is also its accessible name -- not an icon-only X. */
const DISMISS_LABEL = 'Dismiss notice and re-enable Install'
/** The consent body's exit sentence, which describes the Cancel button: present
 *  while Cancel is offered, gone from the permanent refusal whose footer is Close alone. */
const ON_CANCEL = 'Cancel changes nothing'
/** The exit sentence of the GRANTED failure: the grant landed and stands, so Cancel
 *  does not "change nothing" of trust -- the sentence names both facts the reader
 *  leaves with (trusted, switched off) and the generic one must not render beside
 *  a notice that just said the app is now trusted. */
const ON_CANCEL_GRANTED = `Cancel keeps \u201c${DISPLAY}\u201d trusted and switched off`
/** The exit sentence of the PERMANENT refusal, whose footer is Close alone: no
 *  trust decision is left to make, and it says so instead of describing a Cancel
 *  button the reader cannot find. */
const ON_CLOSE_PERMANENT = 'No trust decision is needed here'
/** The disabled Install/Update buttons state the same causality where the user is
 * looking, in the UX lane's words. */
const DISABLED_TITLE = 'Dismiss the notice above to install again'
/** The UPDATE path's copy: the verb follows the action the page offers an
 *  installed app -- Update, beside "Installed version" -- on the banner, the way
 *  back to a retry, the dismiss label and the disabled button's title alike. */
const UPDATE_HEADLINE = "This app can't be updated in the desktop version of Kiro Crew."
const UPDATE_RETRY_PATH = 'Once the author ships the packages, dismiss this notice to update again.'
const UPDATE_DISMISS_LABEL = 'Dismiss notice and re-enable Update'
const UPDATE_DISABLED_TITLE = 'Dismiss the notice above to update again'
/** The narrowest viewport the dashboard supports; the labelled dismiss and the
 *  agent hand-off must not push the banner past it. */
const NARROW = 320

/** The installed record `/api/apps/<name>` answers for the UPDATE path: this
 *  version on disk, the registry row below one ahead of it. Its manifest is the
 *  source's app.json as installed -- `install_app` validates that a description
 *  is present before it writes a record, and the page reads a third-party
 *  installed app's description from here, so the fixture carries one the way a
 *  real record does. */
const INSTALLED_DESCRIPTION = 'A dashboard for your agents, with its own FastAPI backend.'
const INSTALLED_APP = {
  name: APP, displayName: DISPLAY, version: '3.2.6', enabled: true,
  origin: 'registry', resources: 'gateway', lifecycle: 'gateway', source: `registry:${APP}`,
  trustRepository: REPO, installedAt: '2026-09-01T10:00:00Z',
  manifest: {
    name: APP, version: '3.2.6', displayName: DISPLAY,
    description: INSTALLED_DESCRIPTION,
    backend: { entryPoint: 'server.py', type: 'asgi' },
  },
}

const REGISTRY_APP = {
  name: APP, displayName: DISPLAY, version: '3.2.6',
  description: INSTALLED_DESCRIPTION,
  author: 'example-apps', repo: REPO, gitUrl: REPO, trustRepository: REPO,
  tags: ['dashboard'], installed: false, enabled: false, origin: 'registry',
  updateAvailable: false,
  manifest: {
    name: APP, version: '3.2.6', displayName: DISPLAY,
    backend: { entryPoint: 'server.py', type: 'asgi' },
  },
}

/** SSE frames, the shape `installFromRegistryStream` parses: the `log` lines the
 * server streams first, then the one `done` frame. A permanent refusal streams
 * the same sentence it returns, prefixed as `install_from_registry` logs it. */
const sse = (payload, logLines = []) =>
  logLines.map((line) => `event: log\ndata: ${line}\n\n`).join('')
  + `event: done\ndata: ${JSON.stringify(payload)}\n\n`

const DENIED = {
  ok: false, name: APP, code: 'app_execution_denied',
  error: `blocked by execution policy: App ${APP} is not trusted to run its own code.`,
  log: '',
}
const PERMANENT = { ok: false, name: APP, error: REFUSAL, code: REFUSAL_CODE }
/** The line the server streams before the `done` frame -- one whole string per
 *  verb, the verb read from the installed record (`install_from_registry` reads
 *  it once, before its gates run), the same fact the page titles its log panel by. */
const PERMANENT_LOG = {
  install: [`Refusing install: ${REFUSAL}`],
  update: [`Refusing update: ${REFUSAL}`],
}
const TRANSIENT = { ok: false, name: APP, error: TRANSIENT_REASON }

/**
 * What the install stream answers, per attempt, per scenario. An UNTRUSTED app is
 * refused by the execution gate first (which opens the consent modal) and fails
 * on the retry; an already-TRUSTED app fails on its first and only attempt.
 */
const SCENARIOS = {
  permanent: { attempts: [DENIED, PERMANENT] },
  transient: { attempts: [DENIED, TRANSIENT] },
  trusted: { attempts: [PERMANENT] },
  update: { attempts: [PERMANENT], installed: true },
  // The GRANTED failure: an installed, disabled app; Enable is refused by the
  // execution gate (the modal opens), the grant lands, the retried enable fails
  // for an ordinary reason, and the app is still there -- so no rollback, and
  // the modal's `failed` copy (granted=true) renders with the reason beneath.
  granted: { attempts: [], installed: true, disabled: true, enables: [DENIED, { status: 500, json: { error: ENABLE_FAILED_REASON } }] },
}

/** A fresh page with the API stubbed for one scenario. */
async function openPage(context, base, scenario) {
  const page = await context.newPage()
  logPageProblems(page)
  let installs = 0
  let enableCalls = 0
  let untrusts = 0
  page.rollbacks = () => untrusts
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/apps') { await route.fulfill({ json: [] }); return true }
      // 404 is how "no app occupies this name" really arrives: it makes the page
      // load from the registry AND is the rollback probe's proof of absence.
      if (path === `/api/apps/${APP}`) {
        if (SCENARIOS[scenario].installed) {
          await route.fulfill({ json: SCENARIOS[scenario].disabled ? { ...INSTALLED_APP, enabled: false } : INSTALLED_APP })
          return true
        }
        await route.fulfill({ status: 404, json: { error: 'app not installed' } })
        return true
      }
      if (path === '/api/apps/registry') {
        const row = SCENARIOS[scenario].disabled
          ? { ...REGISTRY_APP, installed: true, enabled: false }
          : SCENARIOS[scenario].installed
            ? { ...REGISTRY_APP, installed: true, enabled: true, version: '3.3.0', updateAvailable: true }
            : REGISTRY_APP
        await route.fulfill({
          json: { apps: [row], serverPlatform: { os: 'darwin', arch: 'arm64' } },
        })
        return true
      }
      if (path === '/api/apps/registries') { await route.fulfill({ json: { registries: [] } }); return true }
      if (path === `/api/apps/${APP}/trust` || path === `/api/apps/${APP}/untrust`) {
        if (path.endsWith('/untrust')) untrusts += 1
        await route.fulfill({ json: { ok: true } })
        return true
      }
      if (path === `/api/apps/${APP}/enable` && SCENARIOS[scenario].enables) {
        const { enables } = SCENARIOS[scenario]
        const answer = enables[Math.min(enableCalls, enables.length - 1)]
        enableCalls += 1
        if (answer === DENIED) { await route.fulfill({ status: 403, json: DENIED }); return true }
        await route.fulfill(answer)
        return true
      }
      if (path === '/api/apps/registry/install-stream') {
        const { attempts } = SCENARIOS[scenario]
        const payload = attempts[Math.min(installs, attempts.length - 1)]
        installs += 1
        const logLines = payload === PERMANENT
          ? PERMANENT_LOG[SCENARIOS[scenario].installed ? 'update' : 'install']
          : []
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse(payload, logLines) })
        return true
      }
      return false
    },
  })
  await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
  await page.getByText(DISPLAY).first().waitFor({ timeout: 15000 })
  const action = SCENARIOS[scenario].disabled ? 'Enable' : SCENARIOS[scenario].installed ? 'Update' : 'Install'
  await page.getByRole('button', { name: action, exact: true }).first().click()
  return page
}

/** Drive the consent modal to its failure state and return the modal locator. */
async function failThroughTheModal(page, settledFooter) {
  const modal = page.locator('[role="dialog"]').filter({ hasText: /to run its own code\?/ })
  await modal.waitFor({ timeout: 15000 })
  await modal.getByRole('button', { name: 'Trust this app and enable' }).click()
  await modal.getByRole('alert').waitFor({ timeout: 15000 })
  // Settle the rollback probe + untrust round trip so the copy is final. The
  // footer is the settle signal — and the dialog's own X is also NAMED Close
  // (aria-label), so the footer button is told apart by its visible text.
  await modal.getByRole('button', { name: settledFooter, exact: true })
    .filter({ hasText: settledFooter }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  return modal
}

const fail = (msg, text) => { throw new Error(`${PREFIX}: ${msg}: ${JSON.stringify(text)}`) }

async function capturePermanent(context, base) {
  const page = await openPage(context, base, 'permanent')
  // BEFORE offers Try again in every failure state; AFTER offers Close only for
  // a permanent refusal, because retrying cannot change the verdict.
  const modal = await failThroughTheModal(page, PREFIX === 'before' ? 'Try again' : 'Close')
  const text = await modal.getByRole('alert').innerText()
  // The page's OWN error banner (behind the modal) always carries the sentence —
  // `reportInstallFailure` journals and shows it there. The MODAL is the surface
  // the user is looking at and the one whose footer decides the next action, so
  // the assertions are scoped to the modal and the banner is reported as a fact.
  const pageHasReason = (await page.locator('body').innerText()).includes('bundled interpreter')
  if (PREFIX === 'before') {
    if (!text.includes(GENERIC_BEFORE)) fail('the generic copy must be the whole message', text)
    if (text.includes('bundled interpreter')) {
      fail('the modal already shows the server reason, so this is not the BEFORE state', text)
    }
  } else {
    if (!text.includes(DESKTOP_HEADLINE)) fail('the modal must lead with the desktop headline', text)
    if (!text.includes(DESKTOP_HELP)) fail('the modal must explain the refusal in plain words', text)
    if (!text.includes(GATEWAY_ACTION)) fail('the modal must name the gateway remedy as an action', text)
    if (!text.includes(REFUSAL)) fail("the modal must render the server's reason", text)
    if (text.indexOf(DESKTOP_HELP) > text.indexOf(REFUSAL)) {
      fail("the plain sentence must sit above the server's reason", text)
    }
    if (text.includes(RETRY_INSTRUCTION)) fail('a permanent refusal must not carry the retry instruction', text)
    if (await modal.getByRole('button', { name: 'try again' }).count() !== 0) {
      fail('a permanent refusal must not offer a retry button', text)
    }
    const body = await modal.innerText()
    if (await modal.getByRole('button', { name: 'Cancel', exact: true }).count() !== 0) {
      fail('a permanent refusal offers Close alone, not Cancel', body)
    }
    if (body.includes(ON_CANCEL)) fail('no sentence about Cancel while there is no Cancel button', body)
    if (!body.includes(ON_CLOSE_PERMANENT)) fail('the permanent refusal says no trust decision is left to make', body)
    if (body.includes(ON_CANCEL_GRANTED)) fail('the permanent refusal must not carry the granted state\'s exit sentence', body)
  }
  await modal.screenshot({ path: `${OUT}/${PREFIX}-01-failure.png` })
  await page.close()
  return pageHasReason
}

async function captureTransient(context, base) {
  const page = await openPage(context, base, 'transient')
  const modal = await failThroughTheModal(page, RETRY_BUTTON)
  const text = await modal.getByRole('alert').innerText()
  if (!text.includes(GENERIC)) fail('a transient failure keeps the generic headline as its title', text)
  if (!text.includes(ONE_SWITCH)) fail('the not-granted copy opens by tying trust and "turned on" into one switch', text)
  if (!text.includes(RETRY_INSTRUCTION)) fail('the generic copy must name the retry button as it is labelled', text)
  if (!text.includes(TRANSIENT_REASON)) fail("the modal must render the server's reason beneath it", text)
  if (text.indexOf(GENERIC) > text.indexOf(TRANSIENT_REASON)) {
    fail("the headline must sit above the server's reason", text)
  }
  if (text.includes(DESKTOP_HEADLINE)) fail('a reason without the code must not get the desktop copy', text)
  const body = await modal.innerText()
  if (await modal.getByRole('button', { name: 'Cancel', exact: true }).count() !== 1) {
    fail('a transient failure still offers Cancel', body)
  }
  if (!body.includes(ON_CANCEL)) fail('the sentence describing Cancel stays while Cancel is offered', body)
  await modal.screenshot({ path: `${OUT}/${PREFIX}-02-transient-failure.png` })
  await page.close()
}

async function captureTrustedPage(context, base) {
  const page = await openPage(context, base, 'trusted')
  // The error box is the alert region that leads with the page's headline; the
  // page has other alert regions (the install log among them), and the server's
  // sentence is the log's, not the box's.
  const box = page.getByRole('alert').filter({ hasText: PAGE_HEADLINE })
  await box.first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  if (await page.locator('[role="dialog"]').count() !== 0) {
    fail('an already-trusted app must not open the consent modal', await page.locator('body').innerText())
  }
  const text = await box.first().innerText()
  if (!text.includes(PAGE_HEADLINE)) fail("the page box must lead with the page's own headline", text)
  if (text.includes(DESKTOP_HEADLINE)) fail("the page box must not borrow the modal's app-named headline", text)
  if (!text.includes(DESKTOP_HELP)) fail('the page box must explain the refusal in plain words', text)
  if (!text.includes(GATEWAY_ACTION)) fail('the page box must name the gateway remedy as an action', text)
  // The server's own sentence is the log panel's (streamed verbatim beneath) and
  // the hand-off's (the report), not a third rendering inside the banner.
  if (text.includes(REFUSAL)) fail("the page box must not repeat the server's reason the log shows", text)
  if (!text.includes(RETRY_PATH)) fail('the page box must name the way back to a retry', text)
  const dismiss = box.first().getByRole('button', { name: DISMISS_LABEL, exact: true })
  if (await dismiss.count() !== 1) fail('the page box must offer its dismiss control, named as the retry path', text)
  if (!(await dismiss.innerText()).includes(DISMISS_LABEL)) fail('the dismiss label must be visible text, not only an accessible name', text)
  // One failure surface: the install-log panel is up too, and under this banner
  // its header folds to a plain title -- no second red notice, no second
  // hand-off, no second dismiss anywhere on the page.
  const bodyText = await page.locator('body').innerText()
  if (await page.getByRole('button', { name: 'Dismiss', exact: true }).count() !== 0) {
    fail('no icon-only Dismiss may remain on the page under the banner', bodyText)
  }
  if (await page.getByRole('button', { name: DISMISS_LABEL, exact: true }).count() !== 1) {
    fail('the banner\'s labelled dismiss must be the only dismiss on the page', bodyText)
  }
  if (await page.getByRole('alert').filter({ hasText: 'Install failed' }).count() !== 0) {
    fail('the log panel must not raise a second failure notice under the banner', bodyText)
  }
  if (bodyText.includes('Install failed')) fail('the folded panel names the log, not the outcome', bodyText)
  if (!bodyText.includes('Install log')) fail('the log panel must still be there, titled as the log', bodyText)
  // The log holds what the server streamed -- its refusal line -- never the
  // "Starting install…" placeholder over a finished attempt.
  if (!bodyText.includes(`Refusing install: ${REFUSAL}`)) fail('the log must show the streamed refusal line', bodyText)
  if (bodyText.includes('Starting install')) fail('no "starting" placeholder over a finished install', bodyText)
  if (await page.getByRole('button', { name: 'Ask the agent', exact: true }).count() !== 1) {
    fail('one agent hand-off, the banner\'s', bodyText)
  }
  // Install under a banner that says installing is impossible would run the same
  // clone and build into the same refusal, so it is disabled while the banner is
  // up (the banner's dismiss is how a retry is reached).
  const install = page.getByRole('button', { name: 'Install', exact: true })
  if (await install.count() !== 1) fail('the page must show exactly one Install button', text)
  if (!(await install.isDisabled())) fail('Install must be disabled while the permanent banner is up', text)
  if ((await install.getAttribute('title')) !== DISABLED_TITLE) {
    fail('the disabled Install button must say what re-enables it, at the point of action', text)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-03-trusted-page-banner.png`, fullPage: false })
  // The banner's dismiss clears both surfaces: no "Install failed" (and no
  // "Install complete") left standing beside the re-enabled Install.
  await dismiss.click()
  await page.waitForTimeout(400)
  const after = await page.locator('body').innerText()
  if (after.includes('Install log') || after.includes('Install failed') || after.includes('Install complete')) {
    fail('dismissing the banner must drop the install-log panel with it', after)
  }
  if (await install.isDisabled()) fail('Install must be enabled again once the banner is dismissed', after)
  if (await install.getAttribute('title')) fail('the re-enabled Install button carries no stale title', after)
  await page.close()
}

/** The same banner at the narrowest supported width: the labelled dismiss and the
 *  agent hand-off must not push the banner past the viewport (the German and
 *  French labels are longer still), so the row wraps and the label takes a line
 *  of its own below the message -- one control, one node, at every width. */
async function captureNarrowPage(browser, base) {
  const context = await browser.newContext({
    viewport: { width: NARROW, height: 900 }, deviceScaleFactor: 1, serviceWorkers: 'block',
  })
  const page = await openPage(context, base, 'trusted')
  const box = page.getByRole('alert').filter({ hasText: PAGE_HEADLINE }).first()
  await box.waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  const boxRect = await box.boundingBox()
  if (!boxRect) fail('the banner must be laid out', await page.locator('body').innerText())
  if (boxRect.x < 0 || boxRect.x + boxRect.width > NARROW) {
    fail(`the banner must fit a ${NARROW}px viewport`, boxRect)
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  if (overflow > 0) fail(`the page must not scroll sideways at ${NARROW}px`, { overflow })
  const dismiss = box.getByRole('button', { name: DISMISS_LABEL, exact: true })
  if (await dismiss.count() !== 1) fail('one labelled dismiss', await box.innerText())
  const dismissRect = await dismiss.boundingBox()
  const headline = box.getByText(PAGE_HEADLINE, { exact: false }).first()
  const headlineRect = await headline.boundingBox()
  if (!dismissRect || !headlineRect) fail('the dismiss and the headline must be laid out', await box.innerText())
  if (dismissRect.x + dismissRect.width > boxRect.x + boxRect.width + 1) {
    fail('the labelled dismiss must stay inside the banner', { dismissRect, boxRect })
  }
  if (dismissRect.y < headlineRect.y + headlineRect.height - 1) {
    fail('at this width the labelled dismiss takes its own line below the message', { dismissRect, headlineRect })
  }
  const ask = box.getByRole('button', { name: 'Ask the agent', exact: true })
  const askRect = await ask.boundingBox()
  if (!askRect || askRect.x + askRect.width > boxRect.x + boxRect.width + 1) {
    fail('the agent hand-off must stay inside the banner', { askRect, boxRect })
  }
  await box.screenshot({ path: `${OUT}/${PREFIX}-04-narrow-page-banner.png` })
  await page.close()
  await context.close()
}

/** The UPDATE path: an installed app one version behind offers Update, and the
 *  refusal reached from it takes the update verb everywhere the install path
 *  took the install verb -- the server's streamed line included, since the
 *  server reads the same installed record the page titles its panel by. The
 *  description card is asserted too: it reads the installed record's manifest,
 *  and a blank card beside the refusal would be its own defect. */
async function captureUpdatePage(context, base) {
  const page = await openPage(context, base, 'update')
  const box = page.getByRole('alert').filter({ hasText: UPDATE_HEADLINE })
  await box.first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
  if (await page.locator('[role="dialog"]').count() !== 0) {
    fail('an already-trusted app must not open the consent modal', await page.locator('body').innerText())
  }
  const text = await box.first().innerText()
  if (text.includes(PAGE_HEADLINE) || text.includes("can't be installed")) {
    fail('the update path must not carry the install verb', text)
  }
  if (!text.includes(DESKTOP_HELP)) fail('the update banner must explain the refusal in plain words', text)
  if (!text.includes(UPDATE_RETRY_PATH)) fail('the update banner must name the way back to a retry, in its own verb', text)
  const dismiss = box.first().getByRole('button', { name: UPDATE_DISMISS_LABEL, exact: true })
  if (await dismiss.count() !== 1) fail('the update banner\'s dismiss names Update as what it re-enables', text)
  if (await page.getByRole('button', { name: DISMISS_LABEL, exact: true }).count() !== 0) {
    fail('no install-verbed dismiss on the update path', text)
  }
  const bodyText = await page.locator('body').innerText()
  if (!bodyText.includes('v3.2.6')) fail('the page must show the installed version', bodyText)
  if (!bodyText.includes('Update log')) fail('the log panel under an update-verbed banner is titled for the update', bodyText)
  if (bodyText.includes('Install log')) fail('no install-titled log panel on the update path', bodyText)
  // The server's streamed line carries the same verb as the panel that shows it:
  // both read the installed record, so an "Update log" never holds "Refusing
  // install".
  if (!bodyText.includes(`Refusing update: ${REFUSAL}`)) fail('the log must show the update-verbed streamed refusal line', bodyText)
  if (bodyText.includes('Refusing install')) fail('no install-verbed streamed line on the update path', bodyText)
  // The description card reads the installed record's manifest, exactly as the
  // install path's reads the registry row: not blank because the app is installed.
  if (!bodyText.includes(INSTALLED_DESCRIPTION)) fail('the page must show the installed app\'s description', bodyText)
  const update = page.getByRole('button', { name: 'Update', exact: true })
  if (await update.count() !== 1) fail('the page must show exactly one Update button', bodyText)
  if (!(await update.isDisabled())) fail('Update must be disabled while the permanent banner is up', bodyText)
  if ((await update.getAttribute('title')) !== UPDATE_DISABLED_TITLE) {
    fail('the disabled Update button must say what re-enables it, in its own verb', bodyText)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-05-update-page-banner.png`, fullPage: false })
  await dismiss.click()
  await page.waitForTimeout(400)
  if (await update.isDisabled()) fail('Update must be enabled again once the banner is dismissed', await page.locator('body').innerText())
  if (await update.getAttribute('title')) fail('the re-enabled Update button carries no stale title', await page.locator('body').innerText())
  await page.close()
}

/** The GRANTED failure state: the grant landed and stands (the app is there, so
 *  no rollback), the retried enable failed -- the `failed` copy names the actual
 *  state and the one recovery that applies, with the server's reason beneath. */
async function captureGranted(context, base) {
  const page = await openPage(context, base, 'granted')
  const modal = await failThroughTheModal(page, RETRY_BUTTON)
  const text = await modal.getByRole('alert').innerText()
  if (!text.includes(ONE_SWITCH)) fail('the granted copy opens by tying trust and "turned on" into one switch', text)
  if (!text.includes(GRANTED)) fail('the granted copy must state that the app is now trusted', text)
  if (text.includes(GENERIC)) fail('the granted state must not claim that trust was not saved', text)
  if (!text.includes(RETRY_INSTRUCTION)) fail('the granted copy must name the retry button as it is labelled', text)
  if (!text.includes(REMOVE_TRUST)) fail('the granted copy must offer the one recovery that applies: removing the trust in Settings', text)
  if (!text.includes(ENABLE_FAILED_REASON)) fail("the modal must render the server's reason beneath", text)
  if (text.indexOf(GRANTED) > text.indexOf(ENABLE_FAILED_REASON)) fail('the headline must sit above the reason', text)
  if (page.rollbacks() !== 0) fail('the grant must stand: the app is installed, so nothing is rolled back', text)
  const body = await modal.innerText()
  if (await modal.getByRole('button', { name: 'Cancel', exact: true }).count() !== 1) fail('the granted failure still offers Cancel', body)
  if (await modal.getByRole('button', { name: RETRY_BUTTON, exact: true }).count() !== 1) fail('the granted failure still offers the retry, named for the grant it re-writes', body)
  if (!body.includes(ON_CANCEL_GRANTED)) fail('the granted state names what Cancel leaves: trusted, and switched off', body)
  if (body.includes(ON_CANCEL)) fail('the granted state must not say Cancel changes nothing beside a notice that says the app is now trusted', body)
  if (body.includes(ON_CLOSE_PERMANENT)) fail('the granted state must not carry the permanent refusal\'s exit sentence', body)
  await modal.screenshot({ path: `${OUT}/${PREFIX}-06-trust-saved-failure.png` })
  await page.close()
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1, serviceWorkers: 'block',
  })
  const wrote = [`${PREFIX}-01-failure.png`]
  const pageHasReason = await capturePermanent(context, base)
  if (PREFIX !== 'before') {
    await captureTransient(context, base)
    await captureTrustedPage(context, base)
    await captureNarrowPage(browser, base)
    await captureUpdatePage(context, base)
    await captureGranted(context, base)
    wrote.push(
      `${PREFIX}-02-transient-failure.png`, `${PREFIX}-03-trusted-page-banner.png`,
      `${PREFIX}-04-narrow-page-banner.png`, `${PREFIX}-05-update-page-banner.png`,
      `${PREFIX}-06-trust-saved-failure.png`,
    )
  }
  await browser.close()
  srv.close()
  console.log(
    `Wrote ${wrote.map((f) => `${OUT}/${f}`).join(', ')} (01 asserted: ` +
    `${PREFIX === 'before' ? 'generic copy only, no server reason' : "desktop headline + plain sentence + the server's reason beneath, generic copy gone, Close only"}` +
    `; page banner behind the 01 modal carries the reason: ${pageHasReason})`,
  )
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
