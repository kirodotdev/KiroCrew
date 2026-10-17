/**
 * Screenshots of the paste-block sidecar on ChatPane and SideChat (#11337).
 *
 * Drives the isolated capture entry (website/capture/paste-sidecar.html),
 * which mounts the REAL host. Every frame asserts its state before writing,
 * so a frame cannot document the wrong state:
 *   pane-pill-{dark,light}      ChatPane: a 12-line paste collapsed to `[ Paste #1 · 12 lines ]`
 *   pane-sent-{dark,light}      ChatPane after Enter: the bubble carries the paste chip,
 *                               the composer is empty, the wire text is the expanded paste
 *   side-pill-{dark,light}      SideChat: the same paste collapsed to a token
 *   side-sent-{dark,light}      SideChat after Enter: composer empty, wire text expanded, and the
 *                               sent bubble showing the paste EXPANDED (the side buffer has no meta)
 *   side-toolong-{dark,light}   SideChat: a pill whose EXPANDED text is over the byte limit —
 *                               the error names the collapsed paste, nothing was sent, pill stays
 *
 * Every frame also asserts that no dialog is open on top of the surface.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6843 --strictPort   # in another shell
 *   node scripts/capture-paste-sidecar.mjs http://127.0.0.1:6843 ../temp-screenshots/paste-sidecar-11337
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6843'
const OUT = process.argv[3] || '../temp-screenshots/paste-sidecar-11337'
mkdirSync(OUT, { recursive: true })

const PASTE = Array.from({ length: 12 }, (_, i) => `2026-09-22T10:0${i % 10}:00Z worker-${i} finished step ${i + 1}/12 (ok)`).join('\n')
const TOKEN = '[ Paste #1 · 12 lines ]'

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(host, theme, sent) {
  const page = await browser.newPage({ viewport: host === 'pane' ? { width: 900, height: 640 } : { width: 520, height: 620 }, deviceScaleFactor: 2 })
  // Gateway-free: answer every REAL API call the host makes, and record the
  // send bodies so the frame's caption can be checked against the wire.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (req.method() === 'POST' && (path === '/api/chat' || path.endsWith('/side/turn'))) {
      sent.push(req.postDataJSON())
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, mid: 'm-capture', run_id: 'r-capture', messages: 1 }) })
    }
    if (req.method() === 'POST' && path.endsWith('/side/open')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }) })
    }
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/paste-sidecar.html?host=${host}&theme=${theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; caret-color: transparent !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  return page
}

/** The shipped paste handler, driven by a real clipboard event on the
 *  composer: React's onPaste reads `clipboardData.getData('text')`. */
async function pasteInto(textarea, text) {
  await textarea.focus()
  await textarea.evaluate((el, t) => {
    const dt = new DataTransfer()
    dt.setData('text/plain', t)
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }))
  }, text)
}

/** The visible pill: the composer's highlight layer (or, once sent, the
 *  bubble's PastedChip) renders one `[data-paste-seq]` per collapsed paste. */
async function chips(page, name, scope) {
  const n = await page.locator(`${scope} [data-paste-seq]`).count()
  return check(`${name} chip rendered`, n === 1, `chips=${n} in ${scope}`)
}

async function noDialog(page, name) {
  const dialogs = await page.getByRole('dialog').count()
  return check(`${name} no dialog`, dialogs === 0, `dialogs=${dialogs}`)
}

for (const theme of ['dark', 'light']) {
  // -- ChatPane --
  {
    const sent = []
    const page = await newPage('pane', theme, sent)
    const box = page.locator('textarea[data-composer-input]').first()
    await box.waitFor()
    await box.fill('Turn these into release notes: ')
    await pasteInto(box, PASTE)
    await page.waitForFunction(([sel, tok]) => document.querySelector(sel)?.value.includes(tok), [`textarea[data-composer-input]`, TOKEN])
    const value = await box.inputValue()
    check(`pane-pill-${theme} token`, value.includes(TOKEN) && !value.includes('worker-3'), `value=${JSON.stringify(value.slice(0, 80))}`)
    await chips(page, `pane-pill-${theme}`, '[data-capture-root]')
    await noDialog(page, `pane-pill-${theme}`)
    await page.screenshot({ path: `${OUT}/pane-pill-${theme}.png` })

    await box.press('Enter')
    await page.waitForFunction(() => document.querySelector('textarea[data-composer-input]')?.value === '')
    await page.locator('.user-bubble [data-paste-seq]').first().waitFor({ timeout: 5000 })
    const wire = sent[0]?.message ?? ''
    check(`pane-sent-${theme} wire expanded`, sent.length === 1 && wire.includes('worker-3 finished') && !wire.includes(TOKEN), `sends=${sent.length}`)
    check(`pane-sent-${theme} bubble chip`, Array.isArray(sent[0]?.meta?.pastes) && sent[0].meta.pastes.length === 1, `meta.pastes=${JSON.stringify(sent[0]?.meta?.pastes?.length)}`)
    await chips(page, `pane-sent-${theme}`, '.user-bubble')
    await noDialog(page, `pane-sent-${theme}`)
    await page.screenshot({ path: `${OUT}/pane-sent-${theme}.png` })
    await page.close()
  }
  // -- SideChat --
  {
    const sent = []
    const page = await newPage('side', theme, sent)
    const box = page.locator('[data-side-chat-input] textarea[data-composer-input]')
    await box.waitFor()
    await box.fill('Why did this happen? ')
    await pasteInto(box, PASTE)
    await page.waitForFunction(([sel, tok]) => document.querySelector(sel)?.value.includes(tok), [`[data-side-chat-input] textarea[data-composer-input]`, TOKEN])
    const value = await box.inputValue()
    check(`side-pill-${theme} token`, value.includes(TOKEN) && !value.includes('worker-3'), `value=${JSON.stringify(value.slice(0, 80))}`)
    await chips(page, `side-pill-${theme}`, '[data-side-chat-input]')
    await noDialog(page, `side-pill-${theme}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/side-pill-${theme}.png` })

    await box.press('Enter')
    await page.waitForFunction(() => document.querySelector('[data-side-chat-input] textarea[data-composer-input]')?.value === '')
    // The composer clears on submit, before the open+turn POST chain lands.
    for (let i = 0; i < 50 && sent.length === 0; i++) await page.waitForTimeout(100)
    const wire = sent[0]?.question ?? ''
    // The sent question in the side transcript: the side buffer carries no
    // per-message meta, so the bubble shows the EXPANDED paste — the frame
    // must show that rendering, not only the cleared composer.
    const bubble = page.locator('[data-capture-root] .user-bubble', { hasText: 'worker-3 finished' }).last()
    await bubble.waitFor({ timeout: 5000 })
    await bubble.scrollIntoViewIfNeeded()
    check(`side-sent-${theme} expanded bubble shown`, await bubble.isVisible(), 'sent side bubble renders the pasted lines')
    check(`side-sent-${theme} wire expanded`, sent.length === 1 && wire.includes('worker-3 finished') && !wire.includes(TOKEN), `sends=${sent.length}`)
    await noDialog(page, `side-sent-${theme}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/side-sent-${theme}.png` })
    await page.close()
  }
  // -- SideChat, over the byte limit with a collapsed paste --
  {
    const sent = []
    const page = await newPage('side', theme, sent)
    const box = page.locator('[data-side-chat-input] textarea[data-composer-input]')
    await box.waitFor()
    await box.fill('Why did this happen? ')
    // 33 lines x 1000 chars: past the 32 KiB side-question limit once expanded.
    await pasteInto(box, Array.from({ length: 33 }, (_, i) => `${String(i).padStart(3, '0')} ${'x'.repeat(996)}`).join('\n'))
    await page.waitForFunction((sel) => document.querySelector(sel)?.value.includes('[ Paste #1 · 33 lines ]'), '[data-side-chat-input] textarea[data-composer-input]')
    await box.press('Enter')
    const notice = page.getByText(/counting the collapsed paste/)
    await notice.waitFor({ timeout: 5000 })
    const text = await notice.textContent()
    check(`side-toolong-${theme} error names the paste`, /Question too long — reduce to under ~32,768 characters \(yours: 3\d,\d{3}, counting the collapsed paste\)/.test(text ?? ''), `text=${JSON.stringify(text)}`)
    check(`side-toolong-${theme} nothing sent`, sent.length === 0, `sends=${sent.length}`)
    check(`side-toolong-${theme} pill kept`, (await box.inputValue()).includes('[ Paste #1 · 33 lines ]'), 'composer still holds the pill')
    await chips(page, `side-toolong-${theme}`, '[data-side-chat-input]')
    await noDialog(page, `side-toolong-${theme}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/side-toolong-${theme}.png` })
    await page.close()
  }
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log(`wrote 10 screenshots to ${OUT}`)
