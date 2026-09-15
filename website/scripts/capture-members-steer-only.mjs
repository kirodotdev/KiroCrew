/**
 * Screenshots for the Crew Members DM composer while the member is WORKING.
 *
 * A DM is a conversation with one named member, so it has no queue concept:
 * a send while the member's turn runs is a steer into that turn, and the
 * composer keeps its plain send button. The main chat and split view keep
 * the Steer/Queue split button. Each frame asserts its state before it is
 * written, so a frame cannot document the wrong state.
 *
 *   01-members-busy-composer   Members page, radar mid-turn, text typed:
 *                              plain Send button; no split, no Queue, no stack
 *   02-splitview-busy-composer ChatPane with the default busyMode (split view,
 *                              ⌘D), same state: Steer/Queue split button
 *   03-chatpage-busy-composer  ChatPage, active slot streaming, text typed:
 *                              Steer/Queue split button (untouched)
 *
 * `--expect=before` flips frame 01's assertion to the pre-change state (the
 * warn-coloured "Queue message" button) so the same script photographs the
 * baseline off the base commit.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort
 *   node scripts/capture-members-steer-only.mjs http://127.0.0.1:6833 ../temp-screenshots/members-steer-only [--expect=after|before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6833'
const OUT = positional[1] || '../temp-screenshots/members-steer-only'
const EXPECT = (process.argv.find(a => a.startsWith('--expect=')) || '--expect=after').slice('--expect='.length)
mkdirSync(OUT, { recursive: true })

const THREAD = [
  { role: 'user', content: 'What did you triage tonight?', ts: '2026-08-27T01:00:00Z' },
  { role: 'assistant', content: 'Six new issues so far — walking the open PRs now to see which are already covered.', ts: '2026-08-27T01:00:05Z' },
]

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const routeApi = routeMembersApi

/** The busy composer's controls, read off the live DOM. */
const controls = (page) => page.evaluate(() => ({
  steerOnly: document.querySelectorAll('[data-testid="steer-only-send"]').length,
  split: document.querySelectorAll('[data-testid="busy-send-button"]').length,
  caret: document.querySelectorAll('[data-testid="busy-send-caret"]').length,
  queueBtn: Array.from(document.querySelectorAll('button')).filter(b => b.getAttribute('aria-label') === 'Queue message').length,
  queueStack: document.querySelectorAll('[data-testid="queue-stack"], [aria-label="Cancel queued message"]').length,
}))

// 01 — Members page, radar mid-turn, text typed in the DM composer.
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
  await routeApi(page, { key: 'member-radar', title: 'radar', running: true, messages: THREAD })
  await page.goto(`${BASE}/capture/members-page.html?theme=dark&busy=1`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByText('What did you triage tonight?').waitFor()
  const box = page.locator('[data-chat-pane] textarea').first()
  await box.fill('Also check whether #4213 is a duplicate of last week\'s report.')
  await page.waitForTimeout(150)
  const c = await controls(page)
  if (EXPECT === 'before') {
    check('01-members BEFORE: queue-only button', c.queueBtn === 1 && c.steerOnly === 0 && c.split === 0, JSON.stringify(c))
  } else {
    check('01-members AFTER: plain send, no split/queue', c.steerOnly === 1 && c.split === 0 && c.caret === 0 && c.queueBtn === 0 && c.queueStack === 0, JSON.stringify(c))
    const label = await page.getByTestId('steer-only-send').getAttribute('aria-label')
    check('01-members AFTER: button is named Send', label === 'Send', `aria-label=${label}`)
  }
  await page.screenshot({ path: `${OUT}/01-members-busy-composer-${EXPECT}.png` })
  await page.close()
}

if (EXPECT === 'after') {
  // 02 — split view (⌘D) pane: default busyMode, same busy state.
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
    await routeApi(page, { key: 'chat-1-loader', title: 'radar', running: true, messages: [] })
    await page.goto(`${BASE}/capture/chatpane-loader.html?theme=dark&state=tool_running`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('收到，我扫一遍').waitFor()
    const box = page.locator('[data-chat-pane] textarea').first()
    await box.fill('Also check the CI retry.')
    await page.waitForTimeout(150)
    const c = await controls(page)
    check('02-splitview: Steer/Queue split button kept', c.split === 1 && c.caret === 1 && c.steerOnly === 0, JSON.stringify(c))
    await page.screenshot({ path: `${OUT}/02-splitview-busy-composer.png` })
    await page.close()
  }

  // 03 — main chat (ChatPage), active slot streaming, text typed.
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
    await routeApi(page, { key: 'slot-a', title: 'scroll-shell fixture', running: true, has_more: false, total: 2, messages: [] })
    await page.goto(`${BASE}/capture/chatpage-scroll-shell.html?theme=dark&scene=busy`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('Status check #1:').first().waitFor()
    const box = page.getByLabel('Message input').first()
    await box.fill('Also check the CI retry.')
    await page.waitForTimeout(150)
    const c = await controls(page)
    check('03-chatpage: Steer/Queue split button kept', c.split === 1 && c.caret === 1 && c.steerOnly === 0, JSON.stringify(c))
    await page.screenshot({ path: `${OUT}/03-chatpage-busy-composer.png` })
    await page.close()
  }

  /** Members page with radar mid-turn and the DM open; `chatAnswer` decides
   *  what POST /api/chat answers (a fulfil descriptor, or 'hang' to let the
   *  transport's abort deadline fire). Returns the page and its composer. */
  async function busyDm(chatAnswer) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
    await routeApi(page, { key: 'member-radar', title: 'radar', running: true, messages: THREAD })
    // Registered AFTER routeApi: Playwright consults routes newest-first, so
    // this one wins for /api/chat and the catch-all keeps the rest.
    await page.route(u => new URL(u).pathname === '/api/chat', route => {
      if (chatAnswer === 'hang') return // never answered: SEND_ABORT_MS aborts the POST
      return route.fulfill({ status: 200, contentType: 'application/json', ...chatAnswer })
    })
    await page.goto(`${BASE}/capture/members-page.html?theme=dark&busy=1`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('radar', { exact: true }).first().click()
    await page.getByText('What did you triage tonight?').waitFor()
    return { page, box: page.locator('[data-chat-pane] textarea').first() }
  }
  const STEER_TEXT = 'Also check whether #4213 is a duplicate of last week\'s report.'
  // The transcript only — the composer (and its autosize mirror) also carry the
  // typed text, so a page-wide text count would double-count a restored draft.
  const transcript = (page) => page.locator('[data-chat-pane] .chat-container')

  // 04 — after pressing Send while busy: the text lands at once as an ordinary
  //      right-hand bubble; the server's steer echo then confirms it WITHOUT the
  //      "Steered into the running turn" badge (steer-only hides steer chrome).
  {
    const { page, box } = await busyDm({ body: JSON.stringify({ ok: true, steered: true }) })
    await box.fill(STEER_TEXT)
    await box.press('Enter')
    await page.getByText(STEER_TEXT).waitFor()
    check('04-post-send: composer cleared', (await box.inputValue()) === '', `composer="${await box.inputValue()}"`)
    await page.screenshot({ path: `${OUT}/04-members-after-send-optimistic.png` })
    // The steer_push echo, as the WS would deliver it (harness has no WS).
    await page.evaluate((text) => window.dispatchEvent(new CustomEvent('capture:frame', { detail: { kind: 'steer-echo', slot: 'member-radar', text } })), STEER_TEXT)
    await page.waitForTimeout(200)
    const badge = await page.getByText('Steered into the running turn').count()
    const bubbles = await transcript(page).getByText(STEER_TEXT, { exact: true }).count()
    check('04b-post-send: confirmed steer shows as a plain message, no badge, no duplicate', badge === 0 && bubbles === 1, `badge=${badge} bubbles=${bubbles}`)
    await page.screenshot({ path: `${OUT}/04b-members-after-send-confirmed.png` })
    await page.close()
  }

  // 05 — refused steer: bubble dropped, error row with the server's reason,
  //      draft handed back.
  {
    const { page, box } = await busyDm({ status: 409, body: JSON.stringify({ error: 'slot agent mismatch' }) })
    await box.fill(STEER_TEXT)
    await box.press('Enter')
    await page.getByText(/slot agent mismatch/).waitFor()
    // Settle: the send control re-uses one DOM node across stop -> send, and
    // its `transition-all` colour fade would otherwise be caught mid-frame.
    await page.waitForTimeout(400)
    const restored = (await box.inputValue()) === STEER_TEXT
    const bubbles = await transcript(page).getByText(STEER_TEXT, { exact: true }).count()
    const framed = await transcript(page).getByText(/Couldn't send this message: slot agent mismatch\. Your text is back in the composer\./).count()
    check('05-refused: framed error row + draft restored + no standing bubble', restored && bubbles === 0 && framed === 1, `restored=${restored} bubbles=${bubbles} framed=${framed}`)
    await page.screenshot({ path: `${OUT}/05-members-refused-steer.png` })
    await page.close()
  }

  // 06 — receipt never arrives: after the transport's deadline the bubble is
  //      withdrawn, the draft comes back, and a warn notice says to check the
  //      transcript before resending.
  {
    const { page, box } = await busyDm('hang')
    await box.fill(STEER_TEXT)
    await box.press('Enter')
    await page.getByText(STEER_TEXT).waitFor()
    await page.getByText(/delivery/i).waitFor({ timeout: 15_000 })
    const restored = (await box.inputValue()) === STEER_TEXT
    check('06-response-late: notice + draft restored', restored, `restored=${restored}`)
    await page.screenshot({ path: `${OUT}/06-members-unconfirmed-steer.png` })
    await page.close()
  }

  // 09 — a message the SERVER parked (a backend without a steer channel, a
  //      mid-plan send) on the steer-only surface: the queue card still shows
  //      and is cancellable. The surface never asks for a queue; it does not
  //      hide one that exists.
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
    await routeApi(page, {
      key: 'member-radar', title: 'radar', running: true,
      messages: [...THREAD, { role: 'queued', content: 'And file the duplicate report afterwards.', cls: 'msg msg-queued', ts: '2026-08-27T01:00:09Z', meta: { queueId: 'q-parked' } }],
    })
    await page.goto(`${BASE}/capture/members-page.html?theme=dark&busy=1`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('radar', { exact: true }).first().click()
    await page.getByRole('button', { name: 'Cancel queued message' }).waitFor()
    // Settle the stack's entrance springs (height + fuse margin) before
    // reading geometry: mid-flight the card sits lower than its rest position.
    await page.waitForTimeout(900)
    const c = await controls(page)
    check('09-parked: queue card visible on steer-only, composer still plain', c.queueStack >= 1 && c.split === 0, JSON.stringify(c))
    // The card FUSES onto the composer's top edge; it must never cover the
    // typing area itself.
    const g = await page.evaluate(() => {
      const card = document.querySelector('.queue-card')?.getBoundingClientRect()
      const ta = document.querySelector('[data-chat-pane] textarea')?.getBoundingClientRect()
      return { cardBottom: card ? Math.round(card.bottom) : null, textareaTop: ta ? Math.round(ta.top) : null }
    })
    check('09-parked: card does not cover the typing area', g.cardBottom !== null && g.textareaTop !== null && g.cardBottom <= g.textareaTop, JSON.stringify(g))
    await page.screenshot({ path: `${OUT}/09-members-server-parked-queue-card.png` })
    await page.close()
  }

  // 10 — an upload the server refuses while the member is on screen: the
  //      pane's banner is the shared ErrorNotice (dismissible, no agent
  //      hand-off next to an unsaved draft).
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
    await routeApi(page, { key: 'member-radar', title: 'radar', running: false, messages: THREAD })
    await page.route(u => new URL(u).pathname === '/api/upload/file', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ paths: [], error: 'Unsupported file type: application/x-msdownload' }) }))
    await page.goto(`${BASE}/capture/members-page.html?theme=dark`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('radar', { exact: true }).first().click()
    await page.getByText('What did you triage tonight?').waitFor()
    await page.locator('[data-chat-pane] textarea').first().fill('Here is the tool I mentioned:')
    await page.locator('[data-chat-pane] input[type="file"]').first().setInputFiles({ name: 'tool.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('MZ') })
    await page.getByTestId('chat-pane-upload-error').waitFor()
    const banner = await page.getByTestId('chat-pane-upload-error').textContent()
    const dismiss = await page.getByTestId('chat-pane-upload-error').getByRole('button').count()
    check('10-upload-failure: ErrorNotice banner with reason + dismiss, draft intact', /Unsupported file type/.test(banner || '') && dismiss === 1 && (await page.locator('[data-chat-pane] textarea').first().inputValue()) === 'Here is the tool I mentioned:', `banner="${(banner || '').trim()}" dismiss=${dismiss}`)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/10-members-upload-failure-banner.png` })
    await page.close()
  }

  // 11 — the same refusal landing AFTER the user switched to another member:
  //      no banner over the other thread; an error row in radar's transcript,
  //      found when the user comes back.
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
    await routeApi(page, { key: 'member-radar', title: 'radar', running: false, messages: THREAD })
    let releaseUpload
    await page.route(u => new URL(u).pathname === '/api/upload/file', route => { releaseUpload = () => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ paths: [], error: 'Unsupported file type: application/x-msdownload' }) }) })
    await page.goto(`${BASE}/capture/members-page.html?theme=dark`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('radar', { exact: true }).first().click()
    await page.getByText('What did you triage tonight?').waitFor()
    await page.locator('[data-chat-pane] input[type="file"]').first().setInputFiles({ name: 'tool.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('MZ') })
    await page.waitForTimeout(300)
    await page.getByText('fixer', { exact: true }).first().click()
    await page.waitForTimeout(300)
    releaseUpload()
    await page.waitForTimeout(500)
    const bannerOnFixer = await page.getByTestId('chat-pane-upload-error').count()
    await page.getByText('radar', { exact: true }).first().click()
    await transcript(page).getByText(/Unsupported file type/).waitFor()
    check('11-upload-failure-offscreen: no banner over fixer, error row in radar\'s transcript', bannerOnFixer === 0, `bannerOnFixer=${bannerOnFixer}`)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/11-members-upload-failure-transcript-row.png` })
    await page.close()
  }

  // 12 — recording: per-member draft parking. Type for radar, switch to fixer
  //      (composer empty), type for fixer, back to radar (radar's text is back).
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1, recordVideo: { dir: `${OUT}/.video`, size: { width: 1280, height: 820 } } })
    const page = await ctx.newPage()
    await routeApi(page, { key: 'member-radar', title: 'radar', running: false, messages: THREAD })
    await page.goto(`${BASE}/capture/members-page.html?theme=dark`)
    await page.waitForSelector('[data-capture-root]')
    await page.getByText('radar', { exact: true }).first().click()
    await page.getByText('What did you triage tonight?').waitFor()
    const box = page.locator('[data-chat-pane] textarea').first()
    await box.pressSequentially('Half-typed note for radar…', { delay: 40 })
    await page.waitForTimeout(600)
    await page.getByText('fixer', { exact: true }).first().click()
    await page.waitForTimeout(700)
    const onFixer = await box.inputValue()
    await box.pressSequentially('Something else for fixer', { delay: 40 })
    await page.waitForTimeout(600)
    await page.getByText('radar', { exact: true }).first().click()
    await page.waitForTimeout(700)
    const backOnRadar = await box.inputValue()
    check('12-draft-parking: fixer starts empty, radar text comes back', onFixer === '' && backOnRadar === 'Half-typed note for radar…', `fixer="${onFixer}" radar="${backOnRadar}"`)
    await page.waitForTimeout(600)
    const video = page.video()
    await page.close()
    await ctx.close()
    await video.saveAs(`${OUT}/12-member-switch-draft-parking.webm`)
    await video.delete()
  }

  // 07/08 — recordings: the composer's idle -> busy transition. On the DM the
  //         send button is the SAME element before and after (continuity: the
  //         chat has no busy affordance to swap in); in split view it flips to
  //         the Steer/Queue split button as the main chat does.
  async function recordFlip(name, url, waitText, textareaSel, slot, assertAfter) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1, recordVideo: { dir: `${OUT}/.video`, size: { width: 1280, height: 820 } } })
    const page = await ctx.newPage()
    await routeApi(page, { key: slot, title: 'radar', running: false, messages: THREAD })
    await page.goto(url)
    await page.waitForSelector('[data-capture-root]')
    if (name === '07') await page.getByText('radar', { exact: true }).first().click()
    await page.getByText(waitText).waitFor()
    const box = page.locator(textareaSel).first()
    await box.fill('Also check the CI retry.')
    await page.waitForTimeout(900)
    const before = await controls(page)
    await page.evaluate((s) => window.dispatchEvent(new CustomEvent('capture:frame', { detail: { kind: 'busy', slot: s } })), slot)
    await page.waitForTimeout(1200)
    const after = await controls(page)
    check(`${name}-flip`, assertAfter(before, after), `before=${JSON.stringify(before)} after=${JSON.stringify(after)}`)
    const video = page.video()
    await page.close()
    await ctx.close()
    const webm = `${OUT}/${name}-composer-idle-to-busy.webm`
    await video.saveAs(webm)
    await video.delete()
    // Repo convention (capture-wait-countdown, record-dialog-animation): the
    // webm is the authoritative artifact; a GIF is produced only when a full
    // ffmpeg is on PATH — Playwright's bundled build has no GIF encoder.
    const gif = webm.replace(/\.webm$/, '.gif')
    const gifArgs = ['-y', '-loglevel', 'error', '-i', webm, '-vf', 'fps=10,crop=760:240:in_w-760:in_h-240,split[a][b];[a]palettegen[p];[b][p]paletteuse', '-loop', '0', gif]
    const probe = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' })
    if (probe.error) console.log(`  ffmpeg not on PATH — webm kept as-is. To produce the GIF: ffmpeg ${gifArgs.join(' ')}`)
    else if (spawnSync('ffmpeg', gifArgs, { stdio: 'ignore' }).status === 0) console.log(`  GIF ${gif}`)
    else console.log('  ffmpeg GIF conversion failed; webm kept')
  }
  await recordFlip('07', `${BASE}/capture/members-page.html?theme=dark`, 'What did you triage tonight?', '[data-chat-pane] textarea', 'member-radar',
    (b, a) => b.steerOnly === 0 && b.split === 0 && a.steerOnly === 1 && a.split === 0)
  await recordFlip('08', `${BASE}/capture/chatpane-loader.html?theme=dark&state=idle`, '收到，我扫一遍', '[data-chat-pane] textarea', 'chat-1-loader',
    (b, a) => b.split === 0 && a.split === 1 && a.caret === 1)
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
