/**
 * Screenshot harness + behavior check for the @-menu's search FAILURE states.
 *
 *   1. empty list, timed out       -> `_enter_sends` copy, Retry offered
 *   2. empty list, refusal         -> `_enter_sends` copy, Retry WITHHELD
 *   3. rows survived the failure   -> cause-only copy, Retry offered
 *   4. empty list, generic failure -> `_enter_sends` copy, Retry offered
 *   5. empty list, missing root    -> `_enter_sends` copy, Retry WITHHELD
 *   6. retry in flight             -> cause-only copy, button busy and disabled
 *
 * Scenes 2 and 5 turn on a button that is ABSENT, 3 on rows that are STILL THERE, and
 * 6 on a promise that has been WITHDRAWN, none of which a photograph can evidence, so
 * each is also asserted and a mismatch exits non-zero. Runs against the REAL built SPA
 * (website/dist), so the production retry policy and the handler's `code` contract
 * select the copy rather than a stub of either.
 *
 * Usage: npm run build && node scripts/capture-file-picker-search-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-picker-search-failure'
const SLOT = 'chat-filesearch'
const PROJECT = '/home/user/workspace/notes'
const MESSAGE = 'Check the release notes in @rele'
// A bare `../` token, which is what PATH_TOKEN_RE matches; `@` opens the file picker.
const PATH_MESSAGE = 'Check the release notes in ../notes'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release prep',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Where do the release notes live?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Type `@` and part of the filename.' },
  ],
}

const slotDetail = async (path, route) => {
  if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
  return false
}

const openMenu = async (page, base) => {
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const composer = page.locator('textarea').first()
  await composer.click()
  // Real keydown/input events, so @-detection and the debounced search run as for a typist.
  await composer.pressSequentially(MESSAGE, { delay: 15 })
  return composer
}

// The name is "<label>: <cause>", so an exact "Retry" counts 0 everywhere and
// scene 2's real absence becomes an unfalsifiable pass.
const retryCount = (page) =>
  page.getByRole('button', { name: /^Retry: / }).count()

/**
 * The 403 deliberately carries NO `X-Auth-Required` header: with it the client reads
 * a dashboard-session expiry and falls back to the generic string, so the arm would
 * photograph the wrong copy while still looking like a 403. 'hang' leaves the route
 * handled but unfulfilled, so the production deadline is what aborts the request.
 */
const hintText = page =>
  page.locator('[data-testid="file-picker-search-hint"]').allTextContents()
    .then(all => all.join(' | ')).catch(() => '')

const noticeText = page =>
  page.locator('[data-testid="file-picker-search-error"]').first().textContent()
    .then(t => t || '').catch(() => '')

async function captureEmptyArm(context, base, mode, expected, wantRetry, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path === '/api/file-search') {
      if (mode === 'hang') return true
      if (mode === 'denied') {
        await route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'permission denied', code: 'access_denied' }),
        })
        return true
      }
      if (mode === 'not-found') {
        await route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'project root missing', code: 'project_not_found' }),
        })
        return true
      }
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
      return true
    }
    return slotDetail(path, route)
  }

  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    extra,
    localStorageEntries: { 'mc-active-slot': SLOT },
  })
  await openMenu(page, base)

  // Generous: the hang arm waits out the real deadline (deadline errors are not
  // retried), and the 5xx arm waits out the shared policy's one non-deadline retry.
  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 30000 }).then(() => true).catch(() => false)
  await page.waitForTimeout(400)
  const retries = await retryCount(page)
  const hint = await hintText(page)
  const notice = await noticeText(page)
  const wantFolderHint = mode === 'denied' || mode === 'not-found'
  const noticeClean = !/sends the message/i.test(notice)
  const hintOk = /Enter sends the message/.test(hint)
    && (wantFolderHint
      ? /folder name below the message box/i.test(hint)
      : !/folder name below/i.test(hint))
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  const ok = found && retries === (wantRetry ? 1 : 0) && hintOk && noticeClean
  console.log('wrote', `${OUT}/${shot}.png`,
    { mode, found, retries, wantRetry, notice, noticeClean, hint, wantFolderHint, hintOk, ok })
  await page.close()
  return ok
}

/**
 * Rows survive only on a SAME-key refetch, where the key already holds data; on a new
 * key an error drops the placeholder. The wait is what lets that key go stale, so
 * re-entering it fetches again instead of being served from cache.
 */
async function captureSurvivingRowsArm(context, base, expected, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  let calls = 0
  const extra = async (path, route) => {
    if (path === '/api/file-search') {
      calls += 1
      if (calls === 1) {
        await json(route, {
          results: [{ path: PROJECT + '/src/kept.ts', name: 'kept.ts', size: 12, mtime: 1 }],
          root: PROJECT,
        })
        return true
      }
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
      return true
    }
    return slotDetail(path, route)
  }

  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    extra,
    localStorageEntries: { 'mc-active-slot': SLOT },
  })
  const composer = await openMenu(page, base)
  await page.locator('text=kept.ts').first().waitFor({ timeout: 20000 }).catch(() => {})

  await page.waitForTimeout(11000)
  await composer.press('Backspace')
  await page.waitForTimeout(300)
  await composer.pressSequentially(MESSAGE.slice(-1), { delay: 15 })

  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 20000 }).then(() => true).catch(() => false)
  const rowsSurvived = await page.locator('text=kept.ts').first().isVisible().catch(() => false)
  await page.waitForTimeout(400)
  const retries = await retryCount(page)
  const hint = await hintText(page)
  const hintOk = hint === ''
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  const ok = found && rowsSurvived && retries === 0 && hintOk
  console.log('wrote', `${OUT}/${shot}.png`, { found, rowsSurvived, retries, hint, hintOk, ok })
  await page.close()
  return ok
}

/**
 * The retry is photographed WHILE it runs, so the second answer must never arrive: the
 * press leaves `isFetching` true, which is what drops the notice's "Enter sends" half
 * and puts the button in its busy state.
 */
async function captureRetryInFlightArm(context, base, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  let calls = 0
  const extra = async (path, route) => {
    if (path === '/api/file-search') {
      calls += 1
      if (calls === 1) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
        return true
      }
      return true
    }
    return slotDetail(path, route)
  }

  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    extra,
    localStorageEntries: { 'mc-active-slot': SLOT },
  })
  await openMenu(page, base)

  const retry = page.getByRole('button', { name: /^Retry: / }).first()
  const appeared = await retry.waitFor({ timeout: 30000 }).then(() => true).catch(() => false)
  if (appeared) await retry.click()
  await page.waitForTimeout(1200)

  const busy = await retry.getAttribute('aria-busy').then(v => v === 'true').catch(() => false)
  const disabled = await retry.isDisabled().catch(() => false)
  // Which cause is remembered depends on which keystroke's request lost, so the arm
  // pins the notice and the WITHDRAWN promise rather than one cause's wording.
  const notice = page.locator('[data-testid="file-picker-search-error"]').first()
  const text = await notice.textContent().then(s => (s || '').trim()).catch(() => '')
  const found = /^Search (timed out|failed)$/.test(text)
  const hints = await page.locator('[data-testid="file-picker-search-hint"]')
    .allTextContents().catch(() => [])
  const hintOk = hints.some(t => /won.t send yet/i.test(t))
  // The held copy's own trailing clause is conditional, so only a STANDALONE line would
  // promise a key the retry has swallowed.
  const promisesEnter = hints.some(t => /^Enter sends the message$/.test(t.trim()))
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  const ok = appeared && found && busy && disabled && !promisesEnter && hintOk
  console.log('wrote', `${OUT}/${shot}.png`,
    { appeared, text, found, busy, disabled, promisesEnter, hints, hintOk, ok })
  await page.close()
  return ok
}

/**
 * The path picker is a SECOND FilePickerMenu, opened by a bare `./` or `../` token
 * (PATH_TOKEN_RE) rather than `@`, and it reads /api/path-complete. Its refusal copy has
 * to differ: a blocked subpath leaves the project folder itself reachable, so naming the
 * folder would send the user to a setting that was never wrong.
 */
async function capturePathRefusalArm(context, base, expected, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path.startsWith('/api/path-complete')) {
      await route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'permission denied', code: 'access_denied' }),
      })
      return true
    }
    return slotDetail(path, route)
  }

  await stubDashboardApi(page, {
    slots,
    theme: 'dark',
    extra,
    localStorageEntries: { 'mc-active-slot': SLOT },
  })

  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const composer = page.locator('textarea').first()
  await composer.click()
  await composer.pressSequentially(PATH_MESSAGE, { delay: 15 })

  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 30000 }).then(() => true).catch(() => false)
  await page.waitForTimeout(400)
  const retries = await retryCount(page)
  const hint = await hintText(page)
  const notice = await noticeText(page)
  const hintOk = /pick a different path/i.test(hint)
  const blamesFolder = /project folder/i.test(notice) || /project folder/i.test(hint)
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  const ok = found && retries === 0 && hintOk && !blamesFolder
  console.log('wrote', `${OUT}/${shot}.png`,
    { found, retries, notice, hint, hintOk, blamesFolder, ok })
  await page.close()
  return ok
}
async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const timedOut = await captureEmptyArm(
    context, base, 'hang',
    'Search timed out', true,
    '1-empty-retryable-offers-retry')
  const denied = await captureEmptyArm(
    context, base, 'denied',
    'No access to the project folder', false,
    '2-empty-refusal-withholds-retry')
  const survivingRows = await captureSurvivingRowsArm(
    context, base, 'Search failed',
    '3-surviving-rows-cause-only-notice')
  const failedEmpty = await captureEmptyArm(
    context, base, 'error',
    'Search failed', true,
    '4-empty-generic-failure-offers-retry')
  const rootMissing = await captureEmptyArm(
    context, base, 'not-found',
    'Project folder not found', false,
    '5-empty-root-missing-withholds-retry')
  const retryInFlight = await captureRetryInFlightArm(
    context, base, '6-retry-in-flight-busy')
  const pathRefusal = await capturePathRefusalArm(
    context, base,
    'This path is off limits',
    '7-empty-path-refusal-withholds-retry')

  await context.close()
  await browser.close()
  srv.close()

  const arms = {
    timedOut, denied, survivingRows, failedEmpty, rootMissing, retryInFlight, pathRefusal,
  }
  const failed = Object.entries(arms).filter(([, ok]) => !ok).map(([name]) => name)
  console.log(arms)
  if (failed.length) {
    console.error('FAILED arm(s):', failed.join(', '))
    process.exit(1)
  }
  console.log('all arms rendered their own copy with the Retry affordance the cause permits')
}

await main()
