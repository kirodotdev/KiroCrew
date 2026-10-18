/**
 * Screenshot harness for "Request a Feature" on a host with no kiro-cli.
 *
 * The header's Request-a-Feature action is an agent turn, so a host with no
 * kiro-cli refuses it at session start and the transcript gets the gateway's
 * terminal error row. This scene drives the real flow: it clicks the pill in the
 * built SPA (slot create, seed, send, all answered by stubs), then pushes over
 * the app's own WebSocket the error row the runner appends, and photographs what
 * the user is left with.
 *
 *   node scripts/capture-21626-feature-request-kiro-cli-missing.mjs <outDir> --expect before
 *   node scripts/capture-21626-feature-request-kiro-cli-missing.mjs <outDir> --expect after
 *
 * `before` pushes the row the way the base backend wrote it (no kind) and
 * asserts the dead end: no form link. `after` pushes the row with the
 * `kiro_cli_missing` kind the backend now stamps and asserts the form link, its
 * kiro-cli copy, and no Resume on the card or in the composer, also after a
 * reload. Every frame is asserted from the live DOM before it is written.
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')
const { routeFeatureRequestSlot, sceneChecks } = await import('./lib/feature-request-scene.mjs')

const args = process.argv.slice(2)
const FLAGS = new Set(['--expect'])
const flag = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined }
const OUT = args.find((a, i) => !FLAGS.has(a) && !FLAGS.has(args[i - 1])) || '../temp-screenshots/21626-kiro-cli-missing'
const EXPECT = flag('--expect') || 'after'
if (!['before', 'after'].includes(EXPECT)) throw new Error(`--expect must be before|after, got ${EXPECT}`)

const EXISTING = 'chat-existing'
const FR_SLOT = 'chat-feature-request'
const FORM_URL = 'https://github.com/kirodotdev/KiroCrew/issues/new?template=feature_request.yml'
// Derived from this script's own location, never hardcoded: it renders into the shot.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
// The shape of `kiro_cli_not_found_message`, prefixed the way `chat_runner` does.
const MISSING_ROW = '❌ kiro-cli not found (searched 4 directories: /home/user/.local/bin, /usr/local/bin, /usr/bin, /bin). Install it from https://kiro.dev/cli/, or point KIROCREW_KIRO_BIN at the binary.'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const slot = (key, title, last, messages) => ({
  key, title, running: false, last_message: last, messages, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
})
const existingSlot = slot(EXISTING, 'Parser cleanup', 'Done — both call sites updated.', 2)
const featureRequestSlot = slot(FR_SLOT, 'New chat', '', 0)
const existingDetail = {
  running: false, has_more: false, total: 2, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 600, content: 'Rename getUserName to getUsername across the parser.' },
    { role: 'assistant', ts: now - 560, content: 'Done — both call sites updated.' },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: EXISTING, project: PROJECT, slots: [existingSlot], detail: existingDetail,
    viewport: { width: 1400, height: 950 },
  })
  const { page } = h

  const frRows = await routeFeatureRequestSlot(page, { existingSlot, featureRequestSlot, detail: { running: false, has_more: false, queue: [], project: PROJECT } })
  const { assert, shot, failures } = sceneChecks(page, OUT, EXPECT)

  await h.load('dark', { selector: 'textarea[data-composer-input]', settle: 900 })
  const pill = page.locator('[data-testid="feedback-pill"] button').first()
  assert('feedback pill renders', await pill.count() === 1)
  await pill.click()
  await page.waitForTimeout(1200)
  assert('the flow created and opened its own slot', new URL(page.url()).searchParams.get('sid') === FR_SLOT)
  await page.mouse.move(900, 600)

  const ws = h.ws()
  assert('the app opened its WebSocket', !!ws)
  const meta = EXPECT === 'after' ? { kind: 'kiro_cli_missing', mid: 'e-1' } : { mid: 'e-1' }
  const errorRow = { role: 'error', content: MISSING_ROW, cls: 'msg msg-err', meta }
  frRows.push({ ...errorRow, ts: Date.now() / 1000 })
  ws.send(JSON.stringify({ type: 'chat_message', data: { slot: FR_SLOT, ...errorRow, ts: new Date().toISOString() } }))
  ws.send(JSON.stringify({ type: 'chat_done', data: { slot: FR_SLOT } }))
  await page.waitForTimeout(1200)

  const card = page.locator('[data-testid="error-card"]').first()
  assert('the error row landed in the transcript', await card.count() === 1)
  assert('the row keeps the gateway’s own sentence', /kiro-cli not found/.test(await card.innerText()))
  const link = page.locator('[data-testid="error-card-feature-request-form"]')
  if (EXPECT === 'after') {
    assert('the form link is on the row', await link.count() === 1)
    assert('it points at the feature_request template', await link.getAttribute('href') === FORM_URL)
    const label = await link.innerText()
    console.log('form button:', JSON.stringify(label))
    assert('the button names GitHub and says nothing about monthly usage', /GitHub/.test(label) && !/usage/i.test(label))
    const hint = await page.locator('[data-testid="error-card-feature-request-hint"]').innerText()
    console.log('hint:', JSON.stringify(hint))
    assert('the hint says kiro-cli is not installed and the form needs a GitHub account', /kiro-cli isn't installed/.test(hint) && /GitHub account/.test(hint))
    assert('the hint does not claim the usage is used up', !/usage/i.test(hint))
    assert('no Resume on the card', await page.locator('[data-testid="error-card-continue"]').count() === 0)
    assert('the composer offers no Resume beneath the card', await page.locator('[data-testid="composer-continue"]').count() === 0)
  } else {
    assert('no form route on the row (the dead end)', await link.count() === 0)
  }
  await shot('01-kiro-cli-missing-row-dark')

  await page.emulateMedia({ colorScheme: 'light' })
  await page.evaluate(() => { document.documentElement.classList.remove('dark'); document.documentElement.classList.add('light'); document.documentElement.dataset.theme = 'light' })
  await page.waitForTimeout(400)
  await shot('02-kiro-cli-missing-row-light')

  if (EXPECT === 'after') {
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="error-card"]', { timeout: 20000 })
    await page.waitForTimeout(900)
    assert('after the reload the row still offers the form', await page.locator('[data-testid="error-card-feature-request-form"]').count() === 1)
    assert('after the reload the row still withholds Resume', await page.locator('[data-testid="error-card-continue"]').count() === 0)
    await shot('03-kiro-cli-missing-row-after-reload-dark')
  }

  await h.close()
  console.log(failures() === 0 ? 'ALL ASSERTIONS PASSED' : `${failures()} ASSERTION(S) FAILED`)
  process.exit(failures() === 0 ? 0 : 1)
}

main().catch(err => { console.error(err); process.exit(1) })
