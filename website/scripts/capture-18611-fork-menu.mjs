/** Real-browser evidence for the two fork entries in an assistant message's More menu.
 *
 * Drives website/capture/fork-menu.html: the real AssistantMessage over the literal ChatPage row
 * chain, with the props ChatPage passes for a reply whose fork point resolves on the server
 * (`forkIndex` + `forkMessageId`). `onFork` records its calls. Asserted, then shot:
 *
 *   setting-on       Tail-only Fork on: "Fork with later messages only", then "Fork with earlier
 *                    messages", both with the fork icon, each with a short line under its label
 *                    saying what the new chat holds and that this chat is left as it was; the later
 *                    entry's line opens by saying this message is left out
 *   setting-on-light the same menu in the light theme
 *   setting-off      Tail-only Fork off: the menu is unchanged, "Fork conversation from here" only,
 *                    with no description line
 *   fork-in-flight   after choosing Fork with earlier messages: the spinner takes that entry's icon
 *                    slot so its label keeps its column, both entries are dimmed, the plain entry
 *                    shows no spinner, and clicking either entry starts no second fork
 *   fork-in-flight-later  the same after choosing Fork with later messages only: that entry reads
 *                    Forking… with the spinner, and Fork with earlier messages says another fork is starting
 *   newest-reply     the reply is the newest message: "Fork with later messages only" is held and
 *                    its line says no messages follow yet; "Fork with earlier messages" still forks
 *   refusal-empty-tail  the server refuses Fork with later messages only (`no_messages_after_fork_point`):
 *                    the action-error banner reads "Fork failed: No messages after this one yet."
 *   refusal-too-large   the server refuses Fork with earlier messages (`fork_corpus_too_large`) with the
 *                    setting on: the banner's advice names "Fork with earlier messages"
 * Also asserted with hit-tested clicks (elementFromPoint at the item centre, then a real mouse click):
 *   plain entry -> onFork(1, 'm2')   history entry -> onFork(1, 'm2', 'head')
 * And, with the setting off, that no menu entry wraps onto a second line at 320 px, 390 px or 760 px.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6874 --strictPort
 *   node scripts/capture-18611-fork-menu.mjs http://127.0.0.1:6874 ../temp-screenshots/fork-menu
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6874'
const OUT = resolve(process.argv[3] || '../temp-screenshots/fork-menu')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query, size = { width: 760, height: 600 }) {
  const ctx = await browser.newContext({ viewport: size, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/fork-menu.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[data-role="assistant"]'); await page.waitForTimeout(400)
  return { ctx, page, errors }
}
const forkIds = page => page.getByRole('menuitem').evaluateAll(els => els.map(e => e.getAttribute('data-testid')).filter(id => id && id.startsWith('fork-')))
const text = (page, id) => page.getByTestId(id).evaluate(el => (el.querySelector('[data-testid$="-label"]') ?? el.firstElementChild).innerText.trim())
const hintOf = (page, id) => page.getByTestId(id).innerText().then(t => t.trim())
const present = (page, id) => page.getByTestId(id).count().then(n => n > 0)
const hasIcon = (page, id, name) => page.getByTestId(id).evaluate((el, n) => !!el.querySelector(`svg.lucide-${n}`), name)
/** Left edge of an entry's label text, so a spinner that pushes the label is caught. */
const labelLeft = (page, id) => page.getByTestId(id).evaluate(el => (el.querySelector('[data-testid$="-label"]') ?? el.firstElementChild).lastElementChild.getBoundingClientRect().left)
async function openMore(page) {
  const reply = page.locator('[data-role="assistant"]').first()
  await reply.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(800)
  await reply.getByTestId('assistant-more-actions').click(); await page.waitForTimeout(700)
  await page.waitForSelector('[role="menu"]')
}
/** The item must be what a real pointer lands on at its centre. */
async function hitTested(page, testId) {
  const box = await page.getByTestId(testId).boundingBox()
  const x = box.x + box.width / 2, y = box.y + box.height / 2
  return page.evaluate(([px, py, id]) => !!document.elementFromPoint(px, py)?.closest(`[data-testid="${id}"]`), [x, y, testId])
}
const calls = page => page.evaluate(() => window.__forkCalls)

// 1 setting on
{
  const { ctx, page, errors } = await open('theme=dark&choice=on')
  await openMore(page)
  check('[on] fork entries = plain, then Fork with earlier messages', JSON.stringify(await forkIds(page)) === JSON.stringify(['fork-from-here', 'fork-with-history']))
  check('[on] the plain entry names what the new chat holds', (await text(page, 'fork-from-here')) === 'Fork with later messages only')
  check('[on] the other entry says it keeps them', (await text(page, 'fork-with-history')) === 'Fork with earlier messages')
  check('[on] the plain entry says what the new chat holds, and that this chat is left as it was', (await hintOf(page, 'fork-hint')) === 'Leaves out this message. Only the ones after it go to a new chat. This chat is not changed.')
  check('[on] the other entry says what the new chat holds, and that this chat is left as it was', (await hintOf(page, 'fork-with-history-hint')) === 'Starts a new chat with this message and the ones before it. This chat is not changed.')
  check('[on] both entries draw the fork icon', await hasIcon(page, 'fork-from-here', 'git-fork') && await hasIcon(page, 'fork-with-history', 'git-fork') && !(await hasIcon(page, 'fork-with-history', 'history')))
  const col = [await labelLeft(page, 'fork-from-here'), await labelLeft(page, 'fork-with-history')]
  check('[on] both labels start in one column', Math.abs(col[0] - col[1]) < 1)
  check('[on] no entry is held', (await page.getByTestId('fork-with-history').getAttribute('aria-disabled')) === null && (await page.getByTestId('fork-from-here').getAttribute('aria-disabled')) === null)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-setting-on.png') })
  check('[on] no page errors', errors.length === 0); await ctx.close()
}
// 1b setting on, light theme
{
  const { ctx, page, errors } = await open('theme=light&choice=on')
  check('[light] the page is in the light theme', (await page.evaluate(() => document.documentElement.getAttribute('data-mode'))) === 'light')
  await openMore(page)
  check('[light] fork entries = plain, then Fork with earlier messages', JSON.stringify(await forkIds(page)) === JSON.stringify(['fork-from-here', 'fork-with-history']))
  check('[light] the later entry\'s line opens with what it leaves out', (await hintOf(page, 'fork-hint')) === 'Leaves out this message. Only the ones after it go to a new chat. This chat is not changed.')
  check('[light] the earlier entry\'s line is shown', (await hintOf(page, 'fork-with-history-hint')) === 'Starts a new chat with this message and the ones before it. This chat is not changed.')
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-setting-on-light.png') })
  check('[light] no page errors', errors.length === 0); await ctx.close()
}
// 2 setting off
{
  const { ctx, page, errors } = await open('theme=dark&choice=off')
  await openMore(page)
  check('[off] fork entries = the plain entry only', JSON.stringify(await forkIds(page)) === JSON.stringify(['fork-from-here']))
  check('[off] the plain entry keeps its label', (await text(page, 'fork-from-here')) === 'Fork conversation from here')
  check('[off] the plain entry keeps the fork icon', await hasIcon(page, 'fork-from-here', 'git-fork'))
  check('[off] the menu gains no description line', !(await present(page, 'fork-hint')) && !(await present(page, 'fork-with-history-hint')))
  await page.screenshot({ path: resolve(OUT, 'fork-menu-setting-off.png') })
  check('[off] no page errors', errors.length === 0); await ctx.close()
}
// 3 fork in flight
{
  const { ctx, page, errors } = await open('theme=dark&choice=on&inflight=1')
  await openMore(page)
  check('[inflight] Fork with earlier messages is hit-tested', await hitTested(page, 'fork-with-history'))
  await page.getByTestId('fork-with-history').click(); await page.waitForTimeout(400)
  check('[inflight] the chosen entry calls onFork(1, m2, head) once', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2', 'head']]))
  await openMore(page)
  check('[inflight] Fork with earlier messages is held', (await page.getByTestId('fork-with-history').getAttribute('aria-disabled')) === 'true')
  check('[inflight] the plain entry is held too', (await page.getByTestId('fork-from-here').getAttribute('aria-disabled')) === 'true')
  check('[inflight] the spinner takes the chosen entry\'s icon slot', await hasIcon(page, 'fork-with-history', 'loader-circle') && !(await hasIcon(page, 'fork-with-history', 'git-fork')))
  check('[inflight] the plain entry shows no spinner', !(await hasIcon(page, 'fork-from-here', 'loader-circle')) && await hasIcon(page, 'fork-from-here', 'git-fork'))
  check('[inflight] both labels still start in one column', Math.abs((await labelLeft(page, 'fork-from-here')) - (await labelLeft(page, 'fork-with-history'))) < 1)
  check('[inflight] the chosen entry reads Forking…', (await text(page, 'fork-with-history')).startsWith('Forking…'))
  check('[inflight] the held entry says another fork is starting', (await hintOf(page, 'fork-hint')) === 'Another fork is starting.')
  await page.getByTestId('fork-from-here').click({ force: true }); await page.waitForTimeout(250)
  await page.getByTestId('fork-with-history').click({ force: true }); await page.waitForTimeout(250)
  check('[inflight] clicking either held entry starts no second fork', (await calls(page)).length === 1)
  // Park the pointer outside the menu so no entry carries a hover highlight in the still.
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  const held = await page.getByTestId('fork-with-history').evaluate(el => ({ highlighted: el.hasAttribute('data-highlighted'), dimmed: getComputedStyle(el.querySelector('[data-testid$="-label"]') ?? el.firstElementChild).opacity }))
  check('[inflight] the held entry is dimmed', held.dimmed === '0.5')
  check('[inflight] the plain entry is dimmed too', (await page.getByTestId('fork-from-here').evaluate(el => getComputedStyle(el.firstElementChild).opacity)) === '0.5')
  check('[inflight] both description lines are dimmed with their entries', (await page.getByTestId('fork-hint').evaluate(el => getComputedStyle(el).opacity)) === '0.5' && (await page.getByTestId('fork-with-history-hint').evaluate(el => getComputedStyle(el).opacity)) === '0.5')
  check('[inflight] no entry is highlighted in the still', !held.highlighted)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-fork-in-flight.png') })
  check('[inflight] no page errors', errors.length === 0); await ctx.close()
}
// 3b fork in flight from the later-messages entry
{
  const { ctx, page, errors } = await open('theme=dark&choice=on&inflight=1')
  await openMore(page)
  check('[inflight-later] Fork with later messages only is hit-tested', await hitTested(page, 'fork-from-here'))
  await page.getByTestId('fork-from-here').click(); await page.waitForTimeout(400)
  check('[inflight-later] the chosen entry calls onFork(1, m2) once', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2']]))
  await openMore(page)
  check('[inflight-later] both entries are held', (await page.getByTestId('fork-from-here').getAttribute('aria-disabled')) === 'true' && (await page.getByTestId('fork-with-history').getAttribute('aria-disabled')) === 'true')
  check('[inflight-later] the spinner takes the chosen entry\'s icon slot', await hasIcon(page, 'fork-from-here', 'loader-circle') && !(await hasIcon(page, 'fork-from-here', 'git-fork')))
  check('[inflight-later] Fork with earlier messages shows no spinner', !(await hasIcon(page, 'fork-with-history', 'loader-circle')) && await hasIcon(page, 'fork-with-history', 'git-fork'))
  check('[inflight-later] the chosen entry reads Forking…', (await text(page, 'fork-from-here')).startsWith('Forking…'))
  check('[inflight-later] the other entry says another fork is starting', (await hintOf(page, 'fork-with-history-hint')) === 'Another fork is starting.')
  check('[inflight-later] both labels still start in one column', Math.abs((await labelLeft(page, 'fork-from-here')) - (await labelLeft(page, 'fork-with-history'))) < 1)
  await page.getByTestId('fork-with-history').click({ force: true }); await page.waitForTimeout(250)
  check('[inflight-later] clicking the held entry starts no second fork', (await calls(page)).length === 1)
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-fork-in-flight-later.png') })
  check('[inflight-later] no page errors', errors.length === 0); await ctx.close()
}
// 4 each entry keeps its call shape (not shot)
{
  const { ctx, page, errors } = await open('theme=dark&choice=on')
  await openMore(page)
  check('[plain] the plain entry is hit-tested', await hitTested(page, 'fork-from-here'))
  await page.getByTestId('fork-from-here').click(); await page.waitForTimeout(300)
  check('[plain] calls onFork(1, m2) with no direction', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2']]))
  check('[plain] no page errors', errors.length === 0); await ctx.close()
}
// 4b the newest reply: nothing follows, so the later-messages entry is held with its reason
{
  const { ctx, page, errors } = await open('theme=dark&choice=on&newest=1')
  await openMore(page)
  check('[newest] the later-messages entry is held', (await page.getByTestId('fork-from-here').getAttribute('aria-disabled')) === 'true')
  check('[newest] its line says no messages follow yet', (await hintOf(page, 'fork-hint')) === 'No messages after this one yet.')
  check('[newest] the later-messages entry is dimmed', (await page.getByTestId('fork-from-here').evaluate(el => getComputedStyle(el.firstElementChild).opacity)) === '0.5')
  await page.getByTestId('fork-from-here').click({ force: true }); await page.waitForTimeout(250)
  check('[newest] clicking it starts no fork', (await calls(page)).length === 0)
  check('[newest] Fork with earlier messages is not held', (await page.getByTestId('fork-with-history').getAttribute('aria-disabled')) === null)
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-newest-reply.png') })
  check('[newest] no page errors', errors.length === 0); await ctx.close()
}
// 4c with the setting off the width cap wraps no entry, at the two narrow widths and on a desktop
for (const width of [320, 390, 760]) {
  const { ctx, page, errors } = await open('theme=dark&choice=off&all=1', { width, height: 700 })
  await openMore(page)
  // A label row and each muted line under it are measured on their own: a two-line item such as
  // "Copy as rich text" is a label plus its line by design, not a wrap.
  const rows = await page.getByRole('menuitem').evaluateAll(els => els.flatMap((el) => {
    const id = el.getAttribute('data-testid')
    const label = el.querySelector('.items-center') ?? el
    const out = [{ id, part: 'label', height: label.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(label).lineHeight) || 20 }]
    for (const h of el.querySelectorAll('.text-muted')) out.push({ id, part: 'line', height: h.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(h).lineHeight) || 16 })
    return out
  }))
  console.log(`[off-w${width}] ${rows.map(r => `${r.id}/${r.part} ${r.height.toFixed(1)}/${r.lineHeight}`).join(', ')}`)
  check(`[off-w${width}] the menu lists Speak and Share beside the fork entry`, ['speak-message', 'share-message', 'fork-from-here'].every(id => rows.some(r => r.id === id)))
  check(`[off-w${width}] no entry's label or line wraps`, rows.every(r => r.height < r.lineHeight * 1.5))
  check(`[off-w${width}] no page errors`, errors.length === 0); await ctx.close()
}
// 5 the two widths `narrow-viewport-required` names: the menu stays inside the screen and both description
// lines wrap instead of running past its edge
for (const width of [390, 320]) {
  const { ctx, page, errors } = await open('theme=dark&choice=on', { width, height: 700 })
  await openMore(page)
  const m = await page.getByRole('menu').evaluate((menu) => {
    const box = menu.getBoundingClientRect()
    const lines = Object.fromEntries(['fork-hint', 'fork-with-history-hint'].map((id) => {
      const el = menu.querySelector(`[data-testid="${id}"]`)
      const r = el.getBoundingClientRect()
      return [id, { right: r.right, height: r.height, lineHeight: parseFloat(getComputedStyle(el).lineHeight) }]
    }))
    return { left: box.left, right: box.right, vw: document.documentElement.clientWidth, overflow: menu.scrollWidth - menu.clientWidth, lines }
  })
  console.log(`[w${width}] menu ${m.left.toFixed(1)}..${m.right.toFixed(1)} of ${m.vw}, overflow ${m.overflow}, lines ${JSON.stringify(m.lines)}`)
  check(`[w${width}] the menu starts inside the viewport`, m.left >= 0)
  check(`[w${width}] the menu ends inside the viewport`, m.right <= m.vw)
  check(`[w${width}] the menu keeps 8 px from both screen edges`, m.left >= 8 && m.right <= m.vw - 8)
  check(`[w${width}] the menu has no horizontal overflow`, m.overflow <= 0)
  for (const [id, line] of Object.entries(m.lines)) {
    check(`[w${width}] ${id} wraps onto more than one line`, line.height > line.lineHeight * 1.5)
    check(`[w${width}] ${id} ends inside the menu`, line.right <= m.right)
  }
  check(`[w${width}] both entries are what a pointer lands on`, await hitTested(page, 'fork-from-here') && await hitTested(page, 'fork-with-history'))
  await page.screenshot({ path: resolve(OUT, `fork-menu-narrow-${width}.png`) })
  check(`[w${width}] no page errors`, errors.length === 0); await ctx.close()
}
// 6 server refusals, reported on the page's action-error banner
{
  const { ctx, page, errors } = await open('theme=dark&choice=on&refuse=empty-tail')
  check('[refuse-empty] no banner before a fork', (await page.getByTestId('action-error').count()) === 0)
  await openMore(page)
  await page.getByTestId('fork-from-here').click(); await page.waitForTimeout(600)
  check('[refuse-empty] Fork with later messages only called onFork(1, m2)', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2']]))
  const banner = page.getByTestId('action-error')
  check('[refuse-empty] the banner is shown', (await banner.count()) === 1 && await banner.isVisible())
  check('[refuse-empty] the banner says no messages follow yet', (await banner.innerText()).includes('Fork failed: No messages after this one yet.'))
  check('[refuse-empty] the menu is closed', (await page.getByRole('menu').count()) === 0)
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-refusal-empty-tail.png') })
  check('[refuse-empty] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&choice=on&refuse=too-large')
  await openMore(page)
  await page.getByTestId('fork-with-history').click(); await page.waitForTimeout(600)
  check('[refuse-large] Fork with earlier messages called onFork(1, m2, head)', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2', 'head']]))
  const banner = page.getByTestId('action-error')
  const said = (await banner.count()) ? await banner.innerText() : ''
  check('[refuse-large] the banner is shown', (await banner.count()) === 1 && await banner.isVisible())
  check('[refuse-large] the advice names the chosen entry', said.includes('This conversation is too large to fork. Hover or focus an earlier message and choose “Fork with earlier messages” to copy fewer messages.'))
  check('[refuse-large] the advice does not name the other entry or the setting-off label', !said.includes('Fork with later messages only') && !said.includes('Fork conversation from here'))
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-refusal-too-large.png') })
  check('[refuse-large] no page errors', errors.length === 0); await ctx.close()
}
await browser.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
