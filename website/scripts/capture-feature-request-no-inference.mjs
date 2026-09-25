/**
 * Screenshot harness for "Request a Feature" at the usage limit.
 *
 * The header's Request-a-Feature action is an agent turn by design (the
 * `feature-request` skill drafts and files the issue), so a spent plan
 * allowance refuses it and the transcript gets the gateway's terminal error row.
 * This scene drives the REAL flow rather than preloading a transcript: it clicks
 * the pill in the built SPA (slot create, seed, send -- all answered by stubs),
 * then pushes over the app's own WebSocket the two frames a capped account
 * produces -- the user echo and the error row the runner appends, with the
 * `usage_limit` kind the backend stamps from the raw provider frame -- and
 * photographs what the user is left with.
 *
 * Run it twice against two builds for a before/after pair:
 *
 *   node scripts/capture-feature-request-no-inference.mjs <outDir> --expect before --dist <main-dist>
 *   node scripts/capture-feature-request-no-inference.mjs <outDir> --expect after
 *
 * `--expect` selects the DOM assertions: `before` proves the row is a dead end
 * (no form route, the tooltip promises a feedback action); `after` proves the
 * pill's explanation is real copy a hover paints (linked by aria-describedby,
 * opened under the pill, no "inference"), the row carries the form link with
 * its plain-words explanation and no Resume, and the composer beneath offers
 * no Resume either. Every frame is asserted from the live DOM before it is
 * written, so a shot cannot show a state the assertions did not see.
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')
const { json } = await import('./lib/boot-api.mjs')

const args = process.argv.slice(2)
const FLAGS = new Set(['--expect', '--dist'])
const flag = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined }
// The one positional is the output dir: the first argument that is neither a
// flag nor a flag's value.
const OUT = args.find((a, i) => !FLAGS.has(a) && !FLAGS.has(args[i - 1])) || '../temp-screenshots/feature-request-no-inference'
const EXPECT = flag('--expect') || 'after'
const DIST = flag('--dist')
if (!['before', 'after'].includes(EXPECT)) throw new Error(`--expect must be before|after, got ${EXPECT}`)

const EXISTING = 'chat-existing'
const FR_SLOT = 'chat-feature-request'
const FORM_URL = 'https://github.com/kirodotdev/KiroCrew/issues/new?template=feature_request.yml'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
// The gateway's own row for the reporter's frame, byte for byte what
// `_format_acp_error`'s usage-limit branch returns and `chat_runner` prefixes.
const LIMIT_ROW = '❌ The monthly usage limit has been reached. Retrying will not help until the limit resets. Check your plan\'s usage allowance, or switch to a model or account tier with remaining capacity. (request_id: be06fbe8-3341-4c00-9cac-ff34774e1ec4)'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const existingSlot = {
  key: EXISTING, title: 'Parser cleanup', running: false,
  last_message: 'Done — both call sites updated.', messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}
const featureRequestSlot = {
  key: FR_SLOT, title: 'New chat', running: false, last_message: '', messages: 0,
  agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}
const existingDetail = {
  running: false, has_more: false, total: 2, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 600, content: 'Rename getUserName to getUsername across the parser.' },
    { role: 'assistant', ts: now - 560, content: 'Done — both call sites updated.' },
  ],
}
const emptyDetail = { running: false, has_more: false, total: 0, queue: [], project: PROJECT, messages: [] }

async function main() {
  const h = await openTranscriptHarness({
    slot: EXISTING, project: PROJECT, slots: [existingSlot], detail: existingDetail,
    viewport: { width: 1400, height: 950 }, dist: DIST,
  })
  const { page } = h

  // Registered AFTER the harness's catch-all, so it is consulted first: the
  // pill's three requests get real receipts, everything else falls through.
  // The new slot's detail tracks what the gateway would have PERSISTED by then
  // (the user row on send, the error row once the turn fails): the app re-reads
  // the slot when the turn ends, and that read replaces the transcript.
  let created = false
  const frRows = []
  await page.route('**/api/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path === '/api/chat/slots' && req.method() === 'POST') { created = true; return json(route, featureRequestSlot) }
    if (path === '/api/chat/slots' && req.method() === 'GET') return json(route, created ? [featureRequestSlot, existingSlot] : [existingSlot])
    if (path === `/api/chat/slots/${FR_SLOT}/context`) return json(route, { ok: true })
    if (path.startsWith(`/api/chat/slots/${FR_SLOT}`)) return json(route, { ...emptyDetail, total: frRows.length, messages: frRows })
    // The send: a 2xx JSON receipt with `ok`, which is what "the server accepted
    // the turn" looks like on the wire. The refusal comes later, in the turn.
    // The persisted row keeps the send's `meta` verbatim (`sendId` and the
    // flow's `featureRequest` stamp included), as `chat_handlers.py` does: the
    // stamp is how the transcript recognises the feature-request turn after the
    // chat_done refresh replaces the optimistic bubble, and after a reload.
    if (path === '/api/chat' && req.method() === 'POST') {
      const body = req.postDataJSON()
      frRows.push({ role: 'user', ts: Date.now() / 1000, content: body.message, meta: { ...(body.meta || {}), mid: 'u-1' } })
      return json(route, { ok: true })
    }
    return route.fallback()
  })

  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  const shot = async name => {
    const file = `${OUT}/${EXPECT}-${name}.png`
    await page.screenshot({ path: file })
    console.log('wrote', file)
  }

  await h.load('dark', { selector: 'textarea[data-composer-input]', settle: 900 })

  // 1. The pill, before anything is clicked: what it says the action does. On
  //    the fixed build the explanation is a described-by bubble that a hover
  //    paints (so this frame can show it); on the base build it was a native
  //    `title`, which no screenshot renders.
  const pill = page.locator('[data-testid="feedback-pill"] button').first()
  assert('feedback pill renders', await pill.count() === 1)
  await pill.hover()
  await page.waitForTimeout(400)
  if (EXPECT === 'after') {
    const tip = page.locator('[data-testid="feedback-pill-request-feature-tip"]')
    assert('hovering the pill paints its explanation as real copy', await tip.count() === 1)
    const copy = await tip.innerText()
    console.log('pill explanation:', JSON.stringify(copy))
    assert('the explanation is linked to the button (aria-describedby)', await pill.getAttribute('aria-describedby') === await page.locator('[role="tooltip"]').first().getAttribute('id'))
    assert('it opens under the pill, not off the top of the screen', await page.locator('[role="tooltip"]').first().getAttribute('data-placement') === 'below')
    assert('it names the cost in plain words (monthly usage)', /monthly usage/i.test(copy))
    assert('it names where the request lands (GitHub), not just that it is filed', /files it on GitHub/.test(copy))
    assert('it does not say "inference" (the jargon the blind reader could not parse)', !/inference/i.test(copy))
    assert('no hover-only title remains on the button', (await pill.getAttribute('title')) === null)
  } else {
    const title = await pill.getAttribute('title')
    console.log('pill title:', JSON.stringify(title))
    assert('tooltip does not mention an agent conversation (the dead end this fixes)', !/agent conversation/i.test(title || ''))
  }
  await shot('01-pill-tooltip-dark')

  // 2. Click it: the real flow -- create, seed, send -- against the stubs.
  await pill.click()
  await page.waitForTimeout(1200)
  assert('the flow created and opened its own slot', new URL(page.url()).searchParams.get('sid') === FR_SLOT)
  assert('the optimistic request bubble is on screen', await page.locator('text=I’d like to request a feature!').count() === 1)

  // 3. The turn is refused by the provider: the runner appends the terminal
  //    error row and ends the turn. Same envelope the gateway broadcasts, and
  //    the same row lands in the persisted transcript the app re-reads on done.
  const ws = h.ws()
  assert('the app opened its WebSocket', !!ws)
  const send = frame => ws.send(JSON.stringify(frame))
  const ts = new Date().toISOString()
  const errorRow = { role: 'error', content: LIMIT_ROW, cls: 'msg msg-err', meta: { kind: 'usage_limit', mid: 'e-1' } }
  frRows.push({ ...errorRow, ts: Date.now() / 1000 })
  send({ type: 'chat_message', data: { slot: FR_SLOT, ...errorRow, ts } })
  send({ type: 'chat_done', data: { slot: FR_SLOT } })
  await page.waitForTimeout(1200)

  const card = page.locator('[data-testid="error-card"]').first()
  assert('the error row landed in the transcript', await card.count() === 1)
  assert('the row keeps the provider’s own sentence', /monthly usage limit has been reached/i.test(await card.innerText()))
  assert('exactly one request bubble', await page.locator('text=I’d like to request a feature!').count() === 1)
  const link = page.locator('[data-testid="error-card-feature-request-form"]')
  if (EXPECT === 'after') {
    assert('the form link is on the row', await link.count() === 1)
    assert('it points at the feature_request template', await link.getAttribute('href') === FORM_URL)
    assert('it opens in a new tab', await link.getAttribute('target') === '_blank')
    assert('it carries noopener noreferrer', /noopener/.test(await link.getAttribute('rel') || '') && /noreferrer/.test(await link.getAttribute('rel') || ''))
    assert('the one-line explanation is beside it', await page.locator('[data-testid="error-card-feature-request-hint"]').count() === 1)
    const hint = await page.locator('[data-testid="error-card-feature-request-hint"]').innerText()
    console.log('hint:', JSON.stringify(hint))
    assert('the explanation says the usage is used up, in plain words', /monthly usage is used up/i.test(hint))
    assert('the explanation does not say "inference capacity"', !/inference/i.test(hint))
    // Both routes end on GitHub (the pill's tooltip says "files it on GitHub"),
    // so the hint states which is which: this one skips the agent and costs
    // nothing from the plan.
    assert('the explanation tells the two GitHub routes apart (skips the agent)', /skips the agent/i.test(hint))
    assert('the explanation says the form costs no usage', /none of your monthly usage/i.test(hint))
    assert('no Resume on a row a retry cannot fix', await page.locator('[data-testid="error-card-continue"]').count() === 0)
    // The composer must not argue with the card: no Resume button beneath it,
    // and the placeholder is not the "press Resume, or just type" hint.
    assert('the composer offers no Resume beneath the card', await page.locator('[data-testid="composer-continue"]').count() === 0)
    const placeholder = await page.locator('textarea[data-composer-input]').getAttribute('placeholder')
    console.log('composer placeholder:', JSON.stringify(placeholder))
    assert('the composer placeholder does not urge Resume', !/resume/i.test(placeholder || ''))
  } else {
    assert('no form route on the row (the dead end)', await link.count() === 0)
  }
  await shot('02-usage-limit-row-dark')

  // 4. Light theme, same state.
  await page.emulateMedia({ colorScheme: 'light' })
  await page.evaluate(() => { document.documentElement.classList.remove('dark'); document.documentElement.classList.add('light'); document.documentElement.dataset.theme = 'light' })
  await page.waitForTimeout(400)
  await shot('03-usage-limit-row-light')

  // 5. Reload. The refusal row is persisted server-side, so the transcript
  //    still shows it; the fact that turns it into the form fallback must
  //    survive too, or the reloaded row hands the user a Resume that can only
  //    replay the rejection (Opus finding on the first head). That fact is ON
  //    the persisted user row: the flow stamps `meta.featureRequest: true`
  //    beside the send's `sendId`, and the stub above keeps the send's `meta`
  //    verbatim exactly as `chat_handlers.py` does. Nothing is carried over the
  //    harness's localStorage clear -- this client remembers nothing, which is
  //    also what a second tab looks like.
  if (EXPECT === 'after') {
    const persisted = frRows[0]?.meta || {}
    assert('the persisted user row carries the send\'s sendId', typeof persisted.sendId === 'string' && persisted.sendId.startsWith('s-'))
    assert('the persisted user row carries the feature-request stamp', persisted.featureRequest === true)
    // The slot key itself is `chat-feature-request`, so ordinary per-slot keys
    // (`mc-activity-open:<slot>`) contain the words; the claim is about the
    // retired seed key alone.
    assert('the flow keeps no per-slot seed record on the client', (await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('mc-feature-request-seed:')))).length === 0)
    await page.emulateMedia({ colorScheme: 'dark' })
    // The pointer has sat on the pill since the click; park it over the
    // transcript so the pill's bubble does not ride into a frame about the row.
    await page.mouse.move(900, 600)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="error-card"]', { timeout: 20000 })
    await page.waitForTimeout(900)
    assert('after the reload the slot on screen is still the feature request', new URL(page.url()).searchParams.get('sid') === FR_SLOT)
    assert('after the reload the row still offers the form', await page.locator('[data-testid="error-card-feature-request-form"]').count() === 1)
    assert('after the reload the row still withholds Resume', await page.locator('[data-testid="error-card-continue"]').count() === 0)
    assert('after the reload the composer still offers no Resume', await page.locator('[data-testid="composer-continue"]').count() === 0)
    await shot('04-usage-limit-row-after-reload-dark')
  }

  await h.close()
  console.log(failures === 0 ? 'ALL ASSERTIONS PASSED' : `${failures} ASSERTION(S) FAILED`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch(err => { console.error(err); process.exit(1) })
