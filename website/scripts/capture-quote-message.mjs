/** Real-browser evidence for quoting a whole message.
 *
 * Drives website/capture/quote-message.html (real UserMessage / AssistantMessage /
 * ChatInput + the real useMessageQuote hook). Per theme, and asserted:
 *
 *   1-row       hover on Kiro's newest reply: Regenerate (the seat) + Copy + More
 *   4-sent      (also) the older reply's row is Quote (the seat) + Copy + More
 *   2-menu      right-click on the bubble: the context menu, Quote first
 *   3-composer  the quote staged INSIDE the input box as a card, with Remove
 *   4-sent      Send: the new user row draws the quote card, body without `>`
 *   5-narrow    390px phone frame: long-press menu open over the staged card
 *   6-unstaged  Remove on the card clears it (composer back to plain)
 *   10-user-more      the user row (Quote + Copy + More) with More open (Quote / Copy link / Pin / Edit)
 *   11-reply-more     the main-chat reply's More menu open (Quote / Copy link / Pin / Raw)
 *   12-unavailable    the notice a quote card's failed jump raises
 *   14-copy-failed    a refused clipboard write from the user row's Copy button: the
 *                     ErrorNotice under the row (clipboard stubbed to refuse)
 *   7-crewmate-menu   the crewmate DM (real ChatMessageList + crewmate renderers,
 *                     Reply in thread + Copy in the row): the bubble menu, Quote first
 *   8-crewmate-more   the crewmate reply's More menu: Quote first, then Copy link...
 *   9-crewmate-sent   a DM row carrying the quote card
 *   17-fork-older     a loaded window (Fork as a row button): an older reply
 *                     reads Fork (the seat) + Copy + More
 *   18-fork-newest    the newest reply there: Regenerate (the seat) + Copy +
 *                     Fork + More, so Copy stays second
 *   15-selfquote      the user's OWN message quoted: the sent card and the staged
 *                     card both read "You"
 *   16-queued         a quoting send parked behind a busy turn: the queue card's
 *                     one-line preview is the typed text; 16b its editor open on
 *                     the same text (the quote block stays off the editor)
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6872 --strictPort     # in website/
 *   node scripts/capture-quote-message.mjs http://127.0.0.1:6872 ../temp-screenshots/quote-message
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6872'
const OUT = resolve(process.argv[3] || '../temp-screenshots/quote-message')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(theme, scene, narrow = false, extra = '') {
  const ctx = await browser.newContext({ viewport: narrow ? { width: 390, height: 780 } : { width: 1100, height: 720 }, deviceScaleFactor: 2, hasTouch: narrow, isMobile: narrow })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/quote-message.html?theme=${theme}&scene=${scene}${extra}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('textarea'); await page.waitForTimeout(400)
  return { ctx, page, errors }
}
const shot = (page, name) => page.screenshot({ path: resolve(OUT, name) })

for (const theme of ['dark', 'light']) {
  // 1 row
  {
    const { ctx, page, errors } = await open(theme, 'hover')
    const kiro = page.locator('[data-role="assistant"]').first()
    await kiro.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(800)
    const row = kiro.locator('[data-testid="assistant-more-actions"]').locator('..')
    const labels = await row.locator('> button').evaluateAll(bs => bs.map(b => b.getAttribute('aria-label')))
    check(`[${theme}/row] newest Kiro row = Regenerate + Copy + More`, JSON.stringify(labels) === JSON.stringify(['Regenerate response', 'Copy', 'More actions']))
    await shot(page, `${theme}-1-row.png`)
    check(`[${theme}/row] no page errors`, errors.length === 0); await ctx.close()
  }
  // 2 menu
  {
    const { ctx, page, errors } = await open(theme, 'hover')
    const kiro = page.locator('[data-role="assistant"]').first()
    await kiro.locator('[data-testid="message-bubble"]').click({ button: 'right', position: { x: 140, y: 30 } })
    await page.waitForSelector('[data-testid="message-context-menu"]'); await page.waitForTimeout(200)
    const items = await page.getByRole('menuitem').allInnerTexts()
    // First line only: the copy items carry a hint line under their label.
    check(`[${theme}/menu] Quote first, then both copy formats / Copy link / Pin / Raw`, JSON.stringify(items.map(t => t.split('\n')[0])) === JSON.stringify(['Quote message', 'Copy as Markdown', 'Copy as rich text', 'Copy link to message', 'Pin message', 'Show raw markdown']))
    await shot(page, `${theme}-2-menu.png`)
    // Selecting Quote stages the card in the composer.
    await page.getByTestId('message-context-quote').click()
    await page.waitForSelector('[data-testid="quote-card-composer"]')
    check(`[${theme}/menu] Quote from the menu stages the card`, await page.getByTestId('quote-card-composer').isVisible())
    check(`[${theme}/menu] no page errors`, errors.length === 0); await ctx.close()
  }
  // 3 composer (+ 6 unstaged)
  {
    const { ctx, page, errors } = await open(theme, 'composer')
    const card = page.getByTestId('quote-card-composer')
    const inside = await card.evaluate(el => !!el.closest('[data-testid="input-wrapper"]'))
    check(`[${theme}/composer] card is inside the input box`, inside)
    check(`[${theme}/composer] card names the author + plain excerpt`, (await card.innerText()).includes('Quoting Kiro Crew') && (await card.innerText()).includes('Three things landed in the composer: · Staged files') && !(await card.innerText()).includes('**'))
    await shot(page, `${theme}-3-composer.png`)
    await page.getByTestId('quote-card-remove').click(); await page.waitForTimeout(150)
    check(`[${theme}/composer] Remove clears the card, draft kept`, (await card.count()) === 0 && (await page.locator('textarea[data-composer-input]').inputValue()).startsWith('Can the chips'))
    await shot(page, `${theme}-6-unstaged.png`)
    check(`[${theme}/composer] no page errors`, errors.length === 0); await ctx.close()
  }
  // 4 sent
  {
    const { ctx, page, errors } = await open(theme, 'sent')
    await page.waitForSelector('[data-testid="quote-card-sent"]')
    const sentRow = page.locator('[data-role="user"]').nth(1)
    const bubbleText = await sentRow.locator('.message-bubble').innerText()
    check(`[${theme}/sent] card + body, no raw '>' lines`, bubbleText.includes('Kiro Crew') && bubbleText.includes('Can the chips get a fixed height') && !bubbleText.includes('> '))
    check(`[${theme}/sent] card is the jump control`, await sentRow.getByRole('button', { name: 'Jump to the quoted message' }).isVisible())
    await sentRow.getByRole('button', { name: 'Jump to the quoted message' }).click()
    check(`[${theme}/sent] jump hands over the quoted ts`, (await page.locator('[data-capture-root]').getAttribute('data-jumped')) === '2026-09-29T09:12:40Z')
    const older = page.locator('[data-role="assistant"]').first()
    await older.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(800)
    const olderSeats = await older.getByTestId('assistant-more-actions').locator('..').locator('> button').evaluateAll(bs => bs.map(b => b.getAttribute('aria-label')))
    check(`[${theme}/sent] older Kiro row = Quote + Copy + More`, JSON.stringify(olderSeats) === JSON.stringify(['Quote message', 'Copy', 'More actions']))
    await shot(page, `${theme}-4-sent.png`)
    check(`[${theme}/sent] no page errors`, errors.length === 0); await ctx.close()
  }
  // 5 narrow
  {
    const { ctx, page, errors } = await open(theme, 'narrow', true)
    await page.locator('[data-role="assistant"]').first().locator('[data-testid="message-bubble"]').click({ button: 'right', position: { x: 120, y: 30 } })
    await page.waitForSelector('[data-testid="message-context-menu"]'); await page.waitForTimeout(200)
    check(`[${theme}/narrow] no sideways scroll`, (await page.evaluate(() => document.documentElement.scrollWidth)) <= 390)
    await shot(page, `${theme}-5-narrow.png`)
    check(`[${theme}/narrow] no page errors`, errors.length === 0); await ctx.close()
  }
  // 10 user More / 11 reply More (main chat)
  {
    const { ctx, page, errors } = await open(theme, 'hover')
    const userRow = page.locator('[data-role="user"]').first()
    await userRow.hover({ position: { x: 60, y: 20 } }); await page.waitForTimeout(600)
    const userSeats = await userRow.getByTestId('user-more-actions').locator('..').locator('> button').evaluateAll(bs => bs.map(b => b.getAttribute('aria-label')))
    check(`[${theme}/user-row] user row = Quote + Copy + More`, JSON.stringify(userSeats) === JSON.stringify(['Quote message', 'Copy', 'More actions']))
    await userRow.getByTestId('user-more-actions').click(); await page.waitForTimeout(700)
    check(`[${theme}/user-more] Quote, Copy link, Pin, Edit`, JSON.stringify(await page.getByRole('menuitem').allInnerTexts()) === JSON.stringify(['Quote message', 'Copy link to message', 'Pin message', 'Edit & Resend']))
    await shot(page, `${theme}-10-user-more.png`)
    await page.keyboard.press('Escape'); await page.waitForTimeout(200)
    // 13 the user bubble's own right-click menu
    await userRow.locator('.message-bubble').click({ button: 'right', position: { x: 60, y: 20 } })
    await page.waitForSelector('[data-testid="message-context-menu"]'); await page.waitForTimeout(250)
    check(`[${theme}/user-ctx] Quote first`, JSON.stringify(await page.getByRole('menuitem').allInnerTexts()) === JSON.stringify(['Quote message', 'Copy text', 'Copy link to message', 'Pin message', 'Edit & Resend']))
    await shot(page, `${theme}-13-user-context.png`)
    await page.keyboard.press('Escape'); await page.waitForTimeout(200)
    const kiro = page.locator('[data-role="assistant"]').first()
    await kiro.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(600)
    await kiro.getByTestId('assistant-more-actions').click(); await page.waitForTimeout(700)
    check(`[${theme}/reply-more] Quote, Copy as rich text, Copy link, Pin, Raw`, JSON.stringify((await page.getByRole('menuitem').allInnerTexts()).map(t => t.split('\n')[0])) === JSON.stringify(['Quote message', 'Copy as rich text', 'Copy link to message', 'Pin message', 'Show raw markdown']))
    await shot(page, `${theme}-11-reply-more.png`)
    check(`[${theme}/more] no page errors`, errors.length === 0); await ctx.close()
  }
  // 14 copy failed: stub the clipboard to refuse, press the row's Copy, expect the notice
  {
    const ctx = await browser.newContext({ viewport: { width: 1100, height: 720 }, deviceScaleFactor: 2 })
    await ctx.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('denied')) }, configurable: true })
      document.execCommand = () => false
    })
    const page = await ctx.newPage()
    const errors = []; page.on('pageerror', e => errors.push(String(e)))
    await page.goto(`${BASE}/capture/quote-message.html?theme=${theme}&scene=hover`, { waitUntil: 'networkidle' })
    await page.waitForSelector('textarea'); await page.waitForTimeout(400)
    const userRow = page.locator('[data-role="user"]').first()
    await userRow.hover({ position: { x: 60, y: 20 } }); await page.waitForTimeout(600)
    await userRow.getByRole('button', { name: 'Copy', exact: true }).click(); await page.waitForTimeout(300)
    check(`[${theme}/copy-failed] ErrorNotice under the user row`, (await userRow.getByRole('alert').innerText()).includes('Copy failed'))
    await shot(page, `${theme}-14-copy-failed.png`)
    check(`[${theme}/copy-failed] no page errors`, errors.length === 0); await ctx.close()
  }
  // 15 self-quote: "You" on the sent card and on the staged card
  {
    const { ctx, page, errors } = await open(theme, 'selfquote')
    await page.waitForSelector('[data-testid="quote-card-sent"]'); await page.waitForTimeout(300)
    check(`[${theme}/selfquote] sent card reads You`, (await page.getByTestId('quote-card-sent').innerText()).includes('You'))
    check(`[${theme}/selfquote] staged card reads You`, (await page.getByTestId('quote-card-composer').innerText()).includes('You'))
    await shot(page, `${theme}-15-selfquote.png`)
    check(`[${theme}/selfquote] no page errors`, errors.length === 0); await ctx.close()
  }
  // 16 queued quoting entry: preview, then the editor
  {
    const { ctx, page, errors } = await open(theme, 'queued')
    await page.waitForSelector('[data-testid="queued-host"]'); await page.waitForTimeout(300)
    const host = page.getByTestId('queued-host')
    const preview = await host.innerText()
    check(`[${theme}/queued] preview is the typed text, not the quote`, preview.includes('fixed height') && !preview.includes('quoting an earlier message'))
    await shot(page, `${theme}-16-queued.png`)
    await host.getByLabel('Edit queued message').click(); await page.waitForTimeout(300)
    const value = await host.locator('textarea').inputValue()
    check(`[${theme}/queued-edit] editor opens on the typed text alone`, value.startsWith('Can the chips') && !value.startsWith('>'))
    await shot(page, `${theme}-16b-queued-edit.png`)
    check(`[${theme}/queued] no page errors`, errors.length === 0); await ctx.close()
  }
  // 17-18 loaded window: Fork is a row button
  {
    const { ctx, page, errors } = await open(theme, 'sent', false, '&fork=window')
    await page.waitForSelector('[data-testid="quote-card-sent"]')
    const seatsOf = row => row.getByTestId('assistant-more-actions').locator('..').locator('> button').evaluateAll(bs => bs.map(b => b.getAttribute('data-testid') === 'fork-from-here' ? 'Fork' : b.getAttribute('aria-label')))
    const older = page.locator('[data-role="assistant"]').first()
    await older.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(800)
    check(`[${theme}/fork-older] row = Fork + Copy + More`, JSON.stringify(await seatsOf(older)) === JSON.stringify(['Fork', 'Copy', 'More actions']))
    await shot(page, `${theme}-17-fork-older.png`)
    const newest = page.locator('[data-role="assistant"]').last()
    await newest.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(800)
    check(`[${theme}/fork-newest] row = Regenerate + Copy + Fork + More`, JSON.stringify(await seatsOf(newest)) === JSON.stringify(['Regenerate response', 'Copy', 'Fork', 'More actions']))
    await shot(page, `${theme}-18-fork-newest.png`)
    check(`[${theme}/fork] no page errors`, errors.length === 0); await ctx.close()
  }
  // 12 unavailable notice
  {
    const { ctx, page, errors } = await open(theme, 'unavailable')
    await page.waitForSelector('[data-testid="quote-unavailable-notice"]')
    check(`[${theme}/unavailable] notice text`, (await page.getByTestId('quote-unavailable-notice').innerText()).includes('no longer in this conversation'))
    await shot(page, `${theme}-12-unavailable.png`)
    check(`[${theme}/unavailable] no page errors`, errors.length === 0); await ctx.close()
  }
  // 7-9 crewmate DM
  {
    const { ctx, page, errors } = await open(theme, 'hover', false, '&host=crewmate')
    await page.waitForSelector('[data-testid="crewmate-message"]')
    const bubble = page.locator('[data-testid="crewmate-message"] [data-testid="message-bubble"]').first()
    await bubble.click({ button: 'right', position: { x: 140, y: 30 } })
    await page.waitForSelector('[data-testid="message-context-menu"]'); await page.waitForTimeout(200)
    check(`[${theme}/crewmate] bubble menu, Quote first`, (await page.getByRole('menuitem').first().innerText()).includes('Quote message'))
    const bubbleItems = await page.getByRole('menuitem').allInnerTexts()
    await shot(page, `${theme}-7-crewmate-menu.png`)
    await page.keyboard.press('Escape'); await page.waitForTimeout(150)
    const row = page.locator('[data-testid="crewmate-message"]').first()
    await row.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(700)
    const seats = await row.locator('[data-testid="reply-in-thread"]').locator('..').locator('> button').evaluateAll(bs => bs.map(b => b.getAttribute('aria-label')))
    check(`[${theme}/crewmate] row = Reply + Copy + More`, JSON.stringify(seats) === JSON.stringify(['Reply in thread', 'Copy', 'More actions']))
    // The footer fades in over 300 ms (+100 ms delay) once its menu opens; shoot after it settled.
    await row.getByTestId('assistant-more-actions').click(); await page.waitForTimeout(700)
    check(`[${theme}/crewmate] footer visible while More is open`, parseFloat(await row.getByTestId('assistant-more-actions').evaluate(b => getComputedStyle(b.closest('div')).opacity)) === 1)
    check(`[${theme}/crewmate] More: Quote first`, (await page.getByRole('menuitem').first().innerText()).includes('Quote message'))
    check(`[${theme}/crewmate] More = bubble menu minus the row's Copy`, JSON.stringify(await page.getByRole('menuitem').allInnerTexts()) === JSON.stringify(bubbleItems.filter(i => !i.startsWith('Copy as Markdown'))))
    await shot(page, `${theme}-8-crewmate-more.png`)
    await page.getByTestId('quote-message-menu-item').click()
    await page.waitForSelector('[data-testid="quote-card-composer"]')
    check(`[${theme}/crewmate] Quote from More stages the card`, await page.getByTestId('quote-card-composer').isVisible())
    check(`[${theme}/crewmate] no page errors`, errors.length === 0); await ctx.close()
  }
  {
    const { ctx, page, errors } = await open(theme, 'sent', false, '&host=crewmate')
    await page.waitForSelector('[data-testid="quote-card-sent"]')
    const text = await page.locator('[data-testid="quote-card-sent"]').locator('..').innerText()
    check(`[${theme}/crewmate-sent] card + body, no raw '>'`, text.includes('Can the chips get a fixed height') && !text.includes('> '))
    check(`[${theme}/crewmate-sent] card names the crewmate, not the product`, (await page.getByTestId('quote-card-sent').innerText()).includes('Worker') && !(await page.getByTestId('quote-card-sent').innerText()).includes('Kiro Crew'))
    await page.mouse.move(5, 5); await page.waitForTimeout(300)
    await shot(page, `${theme}-9-crewmate-sent.png`)
    check(`[${theme}/crewmate-sent] no page errors`, errors.length === 0); await ctx.close()
  }
}
await browser.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
