/**
 * Before/after recording for the composer's ↑ recall of a prompt whose send
 * never reached the transcript.
 *
 * Drives the isolated capture entry (capture/recall-history-after-lost-send.html),
 * which mounts the REAL ChatPage. Only the NETWORK is stubbed here, and only at
 * two seams:
 *
 *   POST /api/chat        left PENDING FOREVER. A dead tunnel neither answers nor
 *                         refuses. The route handler simply never fulfils, so the
 *                         request is aborted by sendTurn's OWN AbortController at
 *                         SEND_ABORT_MS (10s) with a real browser AbortError —
 *                         sendTurn returns `response-late`, and ChatPage's branch
 *                         for that status is a bare `return true`: no composer
 *                         restore, ever.
 *   GET  /api/chat/slots/<slot>
 *                         the ORIGINAL transcript, WITHOUT the submitted prompt,
 *                         because the server never received it. The real
 *                         refreshSlot reducer replaces `state.messages` with it
 *                         wholesale, and the optimistic bubble — which carries no
 *                         `meta.mid` — is dropped.
 *
 * Every other claim is ASSERTED against the DOM, not merely photographed. The
 * run exits non-zero if any assertion fails.
 *
 * MODE is the point of the script. `--mode fixed` expects the first ↑ press to
 * return the LOST prompt. `--mode prefix` expects it to return the EARLIER
 * prompt and the lost one to be unreachable by any number of presses. That
 * second run is the negative control: if it passes the `fixed` expectations the
 * harness is not exercising the fix at all, and the recording proves nothing.
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6879 --strictPort        # another shell
 *   node scripts/capture-recall-history.mjs http://127.0.0.1:6879 <outDir> --mode fixed
 *   node scripts/capture-recall-history.mjs http://127.0.0.1:6880 <outDir> --mode prefix
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6879'
const OUT = process.argv[3] || '../temp-screenshots/recall-history'
const MODE = (process.argv.includes('--mode') ? process.argv[process.argv.indexOf('--mode') + 1] : 'fixed')
if (!['fixed', 'prefix'].includes(MODE)) {
  console.error(`--mode must be "fixed" or "prefix" (got ${JSON.stringify(MODE)})`)
  process.exit(1)
}
mkdirSync(OUT, { recursive: true })

const SLOT = 'demo-slot'
/** The prompt under test. Submitted, then lost. NEVER in the fixture transcript. */
const LOST_PROMPT = 'check the deploy status for the staging build'
const PRIOR_PROMPT = 'summarise the release notes'
const PRIOR_REPLY =
  'Three entries: a faster cold start, a fix for duplicate retries, and a new export button.'

/** Must be byte-equal to the entry's own fixture: ChatPage refetches the slot on
 *  mount and the answer REPLACES chat.messages, so a divergent literal here
 *  silently blanks the preloaded transcript. Enforced below, not trusted. */
const FIXTURE = [
  { role: 'user', content: PRIOR_PROMPT, cls: '', ts: '2026-04-02T09:14:00Z', meta: { mid: 'm-1' } },
  { role: 'assistant', content: PRIOR_REPLY, cls: '', ts: '2026-04-02T09:14:06Z', meta: { mid: 'm-2' } },
]

const VIEWPORT = { width: 940, height: 660 }
/** sendTurn's SEND_ABORT_MS is 10_000; clear it so the recording proves the
 *  composer is still empty AFTER the send has definitively given up. */
const PAST_SEND_ABORT_MS = 11_500

const COMPOSER = 'textarea[data-composer-input]'
const READOUT_PROMPTS = '[data-testid="readout-transcript-prompts"]'
const READOUT_ATTEMPTS = '[data-testid="readout-attempts"]'
const RECONNECT = '[data-testid="harness-reconnect"]'

let failures = 0
const check = (name, ok, detail) => {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures += 1
}

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })

/** The whole network simulation, in one place, so the pre-warm page and the
 *  recorded page are answered identically. */
const installRoutes = target => target.route(u => new URL(u).pathname.startsWith('/api/'), route => {
  const req = route.request()
  const path = new URL(req.url()).pathname
  // THE LOST SEND: never fulfilled. sendTurn's own AbortController ends it.
  if (req.method() === 'POST' && path === '/api/chat') return
  if (path === `/api/chat/slots/${SLOT}`) {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        key: SLOT, title: 'release prep', running: false, stopping: false,
        has_more: false, next_before: 0, total: FIXTURE.length, queue: [], messages: FIXTURE,
      }),
    })
  }
  // Bare-array endpoints: their consumers type the response as T[] directly.
  if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) {
    return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  }
  const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$|members|instances|prompts|monitors/.test(path)
  return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
})

// PRE-WARM, in a throwaway page that is NOT recorded. On a cold vite dev server
// the first load transforms the whole module graph, which costs seconds of blank
// pre-paint — and that lands INSIDE the recording, shifting this run's timeline
// against the other mode's. The two GIFs are meant to be compared beat for beat,
// so warm the graph first and let the recorded load be the fast one.
{
  const warm = await browser.newPage({ viewport: VIEWPORT })
  await installRoutes(warm)
  await warm.goto(`${BASE}/capture/recall-history-after-lost-send.html?theme=dark`)
  await warm.waitForSelector(COMPOSER, { timeout: 90_000 })
  await warm.waitForTimeout(400)
  await warm.close()
}

const ctx = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: 1,
  recordVideo: { dir: OUT, size: VIEWPORT },
})
const page = await ctx.newPage()
const pageErrors = []
page.on('pageerror', e => pageErrors.push(String(e).slice(0, 200)))

await installRoutes(page)

/** The transcript's user prompts, EXACTLY as the store holds them — parsed from
 *  the readout the viewer can also read, so assertion and pixels agree. */
const transcriptPrompts = async () => {
  const t = (await page.locator(READOUT_PROMPTS).textContent()) ?? ''
  return t === '(none)' ? [] : t.split('  ·  ')
}
const composerValue = () => page.locator(COMPOSER).inputValue()
const attemptsRecorded = async () => Number((await page.locator(READOUT_ATTEMPTS).textContent()) ?? 'NaN')

console.log(`\nrecall-history capture — mode=${MODE}, base=${BASE}\n`)

await page.goto(`${BASE}/capture/recall-history-after-lost-send.html?theme=dark`)
await page.waitForSelector('[data-capture-root]')
await page.waitForSelector(COMPOSER)
await page.getByText(PRIOR_REPLY).first().waitFor()
await page.waitForTimeout(1200)

// The entry's fixture and this script's route answer must be identical, or the
// mount refetch quietly replaces the transcript with something else.
const parity = await page.evaluate(() => JSON.stringify(window.__CAPTURE_FIXTURE__))
check('fixture-parity entry==script', parity === JSON.stringify(FIXTURE), parity === JSON.stringify(FIXTURE) ? 'ok' : 'entry fixture != script fixture')
// ThemeProvider re-derives the theme from its own preference store; verify the
// requested mode survived mount rather than assuming it.
const themeAttr = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
check('theme applied', !!themeAttr?.endsWith('-dark'), `data-theme=${themeAttr}`)

/* ── 1. the starting state ─────────────────────────────────────────────────── */
check('composer starts empty', (await composerValue()) === '', JSON.stringify(await composerValue()))
{
  const p = await transcriptPrompts()
  check('transcript holds exactly the earlier prompt', JSON.stringify(p) === JSON.stringify([PRIOR_PROMPT]), JSON.stringify(p))
  check('transcript does NOT hold the test prompt', !p.includes(LOST_PROMPT), `prompts=${JSON.stringify(p)}`)
}
check('no submitted prompt recorded yet', (await attemptsRecorded()) === 0, `attempts=${await attemptsRecorded()}`)
await page.screenshot({ path: join(OUT, `01-${MODE}-before.png`) })

/* ── 2. type the prompt and press Enter FOR REAL ───────────────────────────── */
// A synthetic keydown would not exercise the handler; type and press for real.
await page.locator(COMPOSER).click()
await page.locator(COMPOSER).pressSequentially(LOST_PROMPT, { delay: 22 })
check('composer holds the typed prompt', (await composerValue()) === LOST_PROMPT, JSON.stringify(await composerValue()))
await page.screenshot({ path: join(OUT, `02-${MODE}-typed.png`) })
await page.keyboard.press('Enter')
await page.waitForTimeout(900)

/* ── 3. the loss ───────────────────────────────────────────────────────────── */
check('composer cleared on submit', (await composerValue()) === '', JSON.stringify(await composerValue()))
// The reconnect the wipe normally rides in on. Dispatches the REAL refreshSlot.
await page.locator(RECONNECT).click()
await page.waitForTimeout(1400)
{
  const p = await transcriptPrompts()
  check('after refresh, transcript STILL lacks the test prompt', !p.includes(LOST_PROMPT), `prompts=${JSON.stringify(p)}`)
  check('after refresh, transcript is back to exactly the earlier prompt', JSON.stringify(p) === JSON.stringify([PRIOR_PROMPT]), JSON.stringify(p))
}
// And nothing on the page carries it either — the prompt exists nowhere in the UI.
const anywhere = await page.getByText(LOST_PROMPT, { exact: true }).count()
check('the prompt is nowhere on the page', anywhere === 0, `exact-text matches=${anywhere}`)
check('composer still empty right after the wipe', (await composerValue()) === '', JSON.stringify(await composerValue()))
await page.screenshot({ path: join(OUT, `03-${MODE}-lost.png`) })

/* ── 4. past the send abort deadline ───────────────────────────────────────── */
// SEND_ABORT_MS fires at 10s; clearing it proves nothing restores the composer
// on its own — `response-late` is handled with a bare `return true`.
await page.waitForTimeout(PAST_SEND_ABORT_MS)
check('composer STILL empty past the 10s send abort', (await composerValue()) === '', JSON.stringify(await composerValue()))
await page.screenshot({ path: join(OUT, `04-${MODE}-after-abort.png`) })

/* ── 5. press ArrowUp FOR REAL ─────────────────────────────────────────────── */
// Clicking the reconnect button moved focus; put the caret back in the composer
// where a user's caret would be. Empty value ⇒ selectionStart 0, so the handler
// intercepts either way.
await page.locator(COMPOSER).click()
await page.keyboard.press('ArrowUp')
await page.waitForTimeout(900)
const firstRecall = await composerValue()
console.log(`  first ↑ press yielded: ${JSON.stringify(firstRecall)}`)
if (MODE === 'fixed') {
  check('first ↑ recovers the LOST prompt', firstRecall === LOST_PROMPT, JSON.stringify(firstRecall))
  check('one submitted prompt recorded', (await attemptsRecorded()) === 1, `attempts=${await attemptsRecorded()}`)
} else {
  // NEGATIVE CONTROL. Pre-fix, recall history is the transcript alone, so the
  // newest entry is the prompt that DID land and the lost one is not in it.
  check('first ↑ does NOT recover the lost prompt (pre-fix)', firstRecall !== LOST_PROMPT, JSON.stringify(firstRecall))
  check('first ↑ yields the EARLIER prompt instead (pre-fix)', firstRecall === PRIOR_PROMPT, JSON.stringify(firstRecall))
  check('no submitted prompt recorded (pre-fix)', (await attemptsRecorded()) === 0, `attempts=${await attemptsRecorded()}`)
}
await page.screenshot({ path: join(OUT, `05-${MODE}-first-arrowup.png`) })

/* ── 6. a second ArrowUp — ordering, and unreachability pre-fix ────────────── */
await page.keyboard.press('ArrowUp')
await page.waitForTimeout(800)
const secondRecall = await composerValue()
console.log(`  second ↑ press yielded: ${JSON.stringify(secondRecall)}`)
if (MODE === 'fixed') {
  // The unlanded prompt is appended at the TAIL, so the second press steps BACK
  // to the earlier one: existing ordering is not disturbed.
  check('second ↑ reaches the earlier prompt', secondRecall === PRIOR_PROMPT, JSON.stringify(secondRecall))
} else {
  check('second ↑ still does not reach the lost prompt (pre-fix)', secondRecall !== LOST_PROMPT, JSON.stringify(secondRecall))
  // Unreachable by ANY number of presses, not merely by the first two.
  let reached = false
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press('ArrowUp')
    await page.waitForTimeout(280)
    if ((await composerValue()) === LOST_PROMPT) reached = true
  }
  check('lost prompt unreachable after 8 ↑ presses (pre-fix)', !reached, reached ? 'RECOVERED — harness is not exercising the fix' : `settled on ${JSON.stringify(await composerValue())}`)
}
await page.screenshot({ path: join(OUT, `06-${MODE}-second-arrowup.png`) })
await page.waitForTimeout(700)

/* ── 7. nothing internal may reach a published frame ───────────────────────── */
// Exhaustive over TEXT rather than sampled over pixels: a frame can only render
// text the DOM carries, so scanning the whole rendered text catches a leak a
// handful of decoded frames would miss. Kept in the script so a reviewer
// re-running the capture re-runs the check.
{
  const rendered = await page.locator('body').innerText()
  // Shapes, not a denylist of names: a shape also catches the identifier nobody
  // thought to list, and the list itself would publish what it means to withhold.
  const forbidden = [
    /(?:^|\s)\/(?:home|users|workplace|local)\//i, // an absolute path into a checkout
    /\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b/i, // an email address
    /\b[A-Z][A-Za-z]{1,15}-\d{3,}\b/,             // an issue or review identifier
    /\bhttps?:\/\/(?!127\.0\.0\.1|localhost)/i,   // any non-loopback URL
  ]
  const hits = forbidden.filter(re => re.test(rendered)).map(re => re.source)
  check('no internal identifier in the rendered text', hits.length === 0, hits.length ? `matched ${hits.join(', ')}` : `scanned ${rendered.length} chars, clean`)
}

check('no uncaught page errors', pageErrors.length === 0, pageErrors.join(' | ') || 'none')

await page.close()
await ctx.close()

// Playwright names videos by an internal id; give it the reported name.
const target = `${MODE}.webm`
const vid = readdirSync(OUT).find(f => f.endsWith('.webm') && f !== target)
if (vid) renameSync(join(OUT, vid), join(OUT, target))
else check('video written', false, 'no .webm produced')

await browser.close()

console.log(`\nfirst ↑ observed: ${JSON.stringify(firstRecall)}`)
console.log(`second ↑ observed: ${JSON.stringify(secondRecall)}`)
if (failures) {
  console.error(`\nrecall-history capture (${MODE}): ${failures} assertion failure(s)`)
  process.exit(1)
}
console.log(`\nrecall-history capture (${MODE}): all assertions pass — ${join(OUT, target)}`)
