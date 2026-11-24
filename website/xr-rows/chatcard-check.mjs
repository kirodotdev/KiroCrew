// Round 15, the new chat card (CARD-1.md): its rows in the real /xr page, the code of the worktree KiroCrew-card
// from the card worker's own dev server (127.0.0.1:6791), the data of the dev gateway (6777).
// Flat preview with a mouse, unless a row says "session" (the emulated headset, xr-emu: the dot of the head aims and
// a resting hand pinches). The recognizer is a stand-in (this host has no microphone). The card's sends are answered
// in the browser and counted, so that no Bot of the shared dev gateway is talked to; the row `real` alone sends to it.
// Rows: out, words, bot, open, empty, desk, twice, retry, away, nospeech, realmic, real, session, palm, touch
// usage: node chatcard-check.mjs <row>      (writes iterations/iter-15/rows/card-<row>.json)
import { mkdirSync, writeFileSync } from 'node:fs'
import { ITER, bodyOf, cardOf, cardOut, openRoom, pressKey, read, roomOnScreen, say, sleep, throwTo, waitUntil, wholeWindow } from './chatcard-lib.mjs'
import { emu } from './xr-emu/xr-emu-lib.mjs'

const row = process.argv[2] ?? 'out'
const dir = `${ITER}/rows`
const shots = `${ITER}/card`
mkdirSync(dir, { recursive: true }); mkdirSync(shots, { recursive: true })
const out = { row, at: new Date().toISOString(), checks: [], notes: [] }
const check = (name, ok, value) => { out.checks.push({ name, ok: !!ok, value }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${JSON.stringify(value ?? null).slice(0, 500)}`); return !!ok }
const note = (name, value) => { out.notes.push({ name, value }); console.log(`  ${name}: ${JSON.stringify(value).slice(0, 500)}`) }
const near = (a, b, m = 0.01) => a && b && Math.hypot(...a.map((v, i) => v - b[i])) < m
const phaseIs = (page, phase, ms = 6000) => waitUntil(async () => { const c = await cardOf(page); return (phase === null ? c.card === null : c.card?.phase === phase) ? c : null }, ms, 40)
/** A point of the screen over the pages and the Bots: the open. */
const openSky = async page => { const r = await read(page); const head = r.head; return roomOnScreen(page, [head[0] + 0.5, head[1] + 0.25, head[2] - 1.4]) }
const botNamed = async (page, slug) => (await read(page)).crew.find(b => b.slug === slug)
/**
 * A throw with the mouse that seeks its aim (D103 4): the card is pressed, carried to each point of `points` (screen
 * pixels) in turn, and let go at the first one where the build says the card aims as `want` wants (`card.target`).
 * The flat rows aimed at one fixed point: Otto at the picture's edge, or the open sky over Mate's desk (w1087).
 * `hold` runs at the point it is let go at. Returns that point, and every point tried with what the card aimed at.
 */
async function throwSeek(page, points, want, { hold = null } = {}) {
  const c = await cardOf(page)
  const from = await roomOnScreen(page, c.at)
  await page.mouse.move(from.x, from.y); await sleep(120)
  await page.mouse.down(); await sleep(80)
  let at = from
  const tried = []
  for (const to of points) {
    for (let i = 1; i <= 8; i += 1) { await page.mouse.move(at.x + ((to.x - at.x) * i) / 8, at.y + ((to.y - at.y) * i) / 8); await sleep(30) }
    at = to
    await sleep(150)
    // While held the build's aim is the probe's `aim` (`ChatCard.tsx`); `card.target` is set only at the let-go.
    const target = (await cardOf(page)).aim ?? null
    tried.push({ x: Math.round(to.x), y: Math.round(to.y), target })
    if (want(target)) { if (hold) await hold(); await page.mouse.up(); return { to, tried } }
  }
  if (hold) await hold()
  await page.mouse.up()
  return { to: null, tried }
}
const threadOf = (page, slug) => page.evaluate(async s => { const r = await fetch('/v1/crew/owner/bots').then(x => x.json()).catch(() => null); const m = (r?.members ?? []).find(x => x.slug === s || x.name === s); return m?.thread_slot ?? m?.slot_key ?? null }, slug)

const opts = {
  desk: { desk: true, speech: 'none' },
  nospeech: { speech: 'none' },
  real: { catchSends: false },
  retry: { failSends: 1 },
  session: { emulate: true },
  aim70: { emulate: true, clock: true },
  enterlong: { emulate: true },
  palm: { emulate: true },
  touch: { emulate: true, desk: true, speech: 'none' },
  realmic: { speech: 'real' },
}[row] ?? {}
const got = await openRoom(opts)
const { page, sends, slots, errors } = got
try {
  await page.evaluate(() => window.__xrPages.clear())
  await sleep(600)

  if (row === 'out') {
    const before = await cardOf(page)
    check('no card stands before the button is pressed', before.card === null, before.card)
    const c = await cardOut(page)
    check('a press on "new chat" brings one card out, at its place', c.card?.phase === 'out' && near(c.at, c.home), { phase: c.card?.phase, at: c.at, home: c.home })
    const on = await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    check('it says that the microphone is on once the recognizer has started', on && (await page.evaluate(() => window.__recStarts)) === 1, { mic: (await cardOf(page)).card?.mic, starts: await page.evaluate(() => window.__recStarts) })
    const s = await roomOnScreen(page, c.home)
    const box = page.viewportSize()
    check('the card is inside the picture of the start view', s.ahead && s.x > 80 && s.x < box.width && s.y > 90 && s.y < box.height, s)
    await page.click('[data-testid="xr-new-chat"]')
    await sleep(300)
    const again = await cardOf(page)
    check('a second press on the button brings no second card', again.card?.id === c.card.id, { id: again.card?.id })
    await page.screenshot({ path: `${shots}/out.png` })
    // The close mark puts it away, and the recognizer is stopped.
    const r = await read(page)
    const up = [0, 1, 0]
    const toHead = c.home.map((v, i) => r.head[i] - v)
    const right = [up[1] * toHead[2] - up[2] * toHead[1], up[2] * toHead[0] - up[0] * toHead[2], up[0] * toHead[1] - up[1] * toHead[0]]
    const rl = Math.hypot(...right)
    const mark = c.home.map((v, i) => v + (right[i] / rl) * (0.1 - 0.0175) + up[i] * (0.0625 - 0.0175))
    const m = await roomOnScreen(page, mark)
    await page.mouse.click(m.x, m.y)
    const gone = await phaseIs(page, null, 3000)
    check('a press on its close mark puts it away, and nothing is sent', !!gone && sends.length === 0, { card: (await cardOf(page)).card, sends: sends.length })
    check('the recognizer is stopped when the card goes', (await page.evaluate(() => window.__recStops)) >= 1, { stops: await page.evaluate(() => window.__recStops) })
  }

  if (row === 'words') {
    await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, '', 'ship the')
    const a = await waitUntil(async () => { const c = (await cardOf(page)).card; return c.interim === 'ship the' ? c : null }, 2000, 30)
    check('words still worked on show on the card', !!a && a.text === '', a)
    await say(page, 'ship the fix', 'today')
    const b = await waitUntil(async () => { const c = (await cardOf(page)).card; return c.text === 'ship the fix' ? c : null }, 2000, 30)
    check('final words show on the card, with the next ones after them', !!b && b.interim === 'today', b)
    await say(page, 'today', '')
    const c = await waitUntil(async () => { const k = (await cardOf(page)).card; return k.text === 'ship the fix today' ? k : null }, 2000, 30)
    check('runs of words are joined with a space', !!c && c.interim === '', c)
    await say(page, '今天发布', '')
    const d = await waitUntil(async () => { const k = (await cardOf(page)).card; return k.text.endsWith('今天发布') ? k : null }, 2000, 30)
    check('words of a script with no spaces are kept as they are', !!d, d?.text)
    await page.screenshot({ path: `${shots}/words.png` })
    check('the keyboard does not show while the microphone is on', (await cardOf(page)).keys === null, null)
  }

  if (row === 'bot') {
    // The Bots stay where they stand (the room of places lets them stroll). The Bot thrown at is the one in view that
    // stands farthest from the card's own place at the eye: in the room of places Juno stands 5 degrees from it
    // (w1085), where a mouse's aim on it put the card back.
    await page.evaluate(() => window.__xrPages.stroll?.(false))
    await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'check the release notes', '')
    await sleep(200)
    const id = (await cardOf(page)).card.id
    const k0 = await cardOf(page); const e0 = (await read(page)).head
    const ang = (p, q) => { const a = p.map((v, i) => v - e0[i]); const c = q.map((v, i) => v - e0[i]); const d = Math.hypot(...a) * Math.hypot(...c); return +((Math.acos(Math.max(-1, Math.min(1, (a[0] * c[0] + a[1] * c[1] + a[2] * c[2]) / d))) * 180) / Math.PI).toFixed(1) }
    const seen = []
    for (const b of (await read(page)).crew) { const s = await roomOnScreen(page, bodyOf(b)); if (s.ahead && s.x > 120 && s.x < 1380 && s.y > 120 && s.y < 860) seen.push({ b, s, deg: ang(bodyOf(b), k0.home) }) }
    seen.sort((p, q) => q.deg - p.deg)
    note('the Bots in view, by how far each stands from the card\'s place', seen.map(x => `${x.b.slug}:${x.deg}`))
    // Points of each Bot's body, farthest from the card's place first: where its body is now (`parts`) and where it
    // stands (`feet`), middle first. The Bot thrown at is the first the card aims at.
    const points = []
    for (const { b } of seen) {
      const base = b.parts ?? b.feet; const tall = b.top[1] - b.feet[1]
      for (const k of [0.5, 0.35, 0.65]) for (const p of [[base[0], base[1] + tall * k, base[2]], [b.feet[0], b.feet[1] + tall * k, b.feet[2]]]) { const s = await roomOnScreen(page, p); if (s.ahead) points.push(s) }
    }
    let held = null
    const seek = await throwSeek(page, points, t => t?.to === 'bot', { hold: async () => { held = await cardOf(page); await page.screenshot({ path: `${shots}/held-bot.png` }) } })
    note('where the throw was let go, and what the card aimed at on the way', seek)
    const aimed = seek.tried.at(-1)?.target
    const bot = (aimed?.to === 'bot' ? await botNamed(page, aimed.slug) : null) ?? seen[0]?.b ?? await botNamed(page, 'juno')
    const thread = await threadOf(page, bot.slug)
    check('held over a Bot, the card rides at the pointer and has left its place', held.card.phase === 'held' && !near(held.at, held.home, 0.05), { phase: held.card.phase, at: held.at, target: held.card.target, grip: held.grip })
    const fly = await waitUntil(async () => { const k = await cardOf(page); return k.card && k.card.phase !== 'held' ? k : null }, 2000, 10)
    check('let go on the Bot, it flies to that Bot', ['flying', 'sending', 'sent'].includes(fly.card?.phase) && fly.card.target?.to === 'bot' && fly.card.target.slug === bot.slug, fly.card)
    await page.screenshot({ path: `${shots}/fly-bot.png` })
    await phaseIs(page, null, 5000)
    await sleep(600)
    const end = await cardOf(page)
    // The message is the card's words, then the XR line that asks for a short first answer (`writes.ts` `xrMessage`).
    check('one send goes out, to that Bot\'s own thread, with the card\'s words', sends.length === 1 && sends[0].message?.startsWith('check the release notes\n\n') && !!thread && sends[0].slot === thread, { sends, thread })
    check('the send carries the card\'s one id', sends[0]?.key === `send-${id}`, { key: sends[0]?.key, id })
    check('no new chat is started for it', slots.length === 0, slots.length)
    check('the card is gone after it was sent, and the count says one', end.card === null && end.sends === 1 && end.sent === 1, { card: end.card, sends: end.sends, sent: end.sent, slot: end.slot })
    check('no page opened for a card that a Bot took', (await read(page)).panels.length === 0, (await read(page)).panels.map(p => p.id))
    check('the recognizer was stopped at the throw', (await page.evaluate(() => window.__recStops)) >= 1, await page.evaluate(() => window.__recStops))
  }

  if (row === 'open') {
    await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'what changed this week', '')
    await sleep(200)
    // Points of the sky over the pages, the Bots and the desks: the first where the card aims into the open.
    const r0 = await read(page); const h0 = r0.head
    const points = []
    for (const [dx, dy] of [[0.5, 0.25], [0.5, 0.6], [-0.5, 0.6], [0, 0.9], [0.9, 0.9], [-0.9, 0.9]]) { const s = await roomOnScreen(page, [h0[0] + dx, h0[1] + dy, h0[2] - 1.4]); if (s.ahead) points.push(s) }
    let held = null
    const seek = await throwSeek(page, points, t => t?.to === 'open', { hold: async () => { held = await cardOf(page); await page.screenshot({ path: `${shots}/held-open.png` }) } })
    note('where the throw was let go, and what the card aimed at on the way', seek)
    const fly = await waitUntil(async () => { const k = await cardOf(page); return k.card && k.card.phase !== 'held' ? k : null }, 2000, 10)
    check('let go over no Bot, it flies into the open', fly.card?.target?.to === 'open' && ['flying', 'sending', 'sent'].includes(fly.card.phase), fly.card)
    await phaseIs(page, null, 5000)
    await sleep(800)
    check('one new chat is started', slots.length === 1, slots)
    check('one send goes out, to that new chat, with the card\'s words', sends.length === 1 && sends[0].slot === 'cardtest1' && sends[0].message?.startsWith('what changed this week\n\n'), sends)
    const r = await read(page)
    const paths = Object.values(r.sources).map(s => s.path)
    check('the new chat\'s page opens on a panel', paths.some(p => p === '/chat?sid=cardtest1'), paths)
    await page.screenshot({ path: `${shots}/open-after.png` })
  }

  if (row === 'empty') {
    const bot = await botNamed(page, 'mira')
    await cardOut(page)
    const to = await roomOnScreen(page, bodyOf(bot))
    await throwTo(page, to)
    const c = await phaseIs(page, 'typing', 3000)
    check('an empty card that is thrown is not sent', sends.length === 0 && slots.length === 0, { sends: sends.length })
    check('it stands at its place again, with its target kept', !!c && c.card.target?.slug === 'mira', c?.card)
    await waitUntil(async () => near((await cardOf(page)).at, c.home, 0.01), 3000, 50)
    const k = await cardOf(page)
    check('the keyboard comes: it stands in front, under the card', !!k.keys && k.keys.flat === false && k.keys.at[1] < k.home[1], k.keys && { at: k.keys.at, flat: k.keys.flat, layer: k.keys.layer })
    check('the recognizer is stopped while he types', (await page.evaluate(() => window.__recStops)) >= 1, await page.evaluate(() => window.__recStops))
    await page.screenshot({ path: `${shots}/empty-keys.png` })
    // Enter with no words sends nothing.
    await pressKey(page, 'Enter')
    await sleep(300)
    check('Enter on the empty card sends nothing', sends.length === 0 && (await cardOf(page)).card.phase === 'typing', (await cardOf(page)).card.phase)
    // Typed with the mouse on the room's keys: a capital, small letters, a space, a digit, a backspace.
    for (const key of ['Shift']) await pressKey(page, key)
    for (const key of ['H', 'i', ' ', 'm', 'i', 'r', 'a', 'x', 'Backspace', 'Layer']) await pressKey(page, key)
    for (const key of ['2', 'Layer']) await pressKey(page, key)
    const typed = (await cardOf(page)).card.text
    check('words typed on the room\'s keys show on the card', typed === 'Hi mira2', typed)
    // A real keyboard types on the card too.
    await page.keyboard.type(' ok')
    await sleep(200)
    check('a real keyboard types on the card while its keyboard shows', (await cardOf(page)).card.text === 'Hi mira2 ok', (await cardOf(page)).card.text)
    await page.screenshot({ path: `${shots}/empty-typed.png` })
    await pressKey(page, 'Enter')
    await phaseIs(page, null, 5000)
    await sleep(500)
    check('Enter sends it once, to the Bot it was thrown at', sends.length === 1 && sends[0].message?.split('\n\n')[0] === 'Hi mira2 ok', sends)
    check('the keyboard goes with the card', (await cardOf(page)).keys === null, null)
  }

  if (row === 'desk') {
    const r0 = await read(page)
    const desk = r0.desk
    const c = await cardOut(page)
    check('with desk=1 and no recognizer the keyboard comes at once', !!c.keys, c.keys && { at: c.keys.at, flat: c.keys.flat })
    const k = (await cardOf(page)).keys
    const ys = Object.values(k.centers).map(p => p[1])
    check('it lies flat on the desk\'s top', k.flat === true && ys.every(y => Math.abs(y - desk.at[1]) < 0.005), { deskY: desk.at[1], keysY: [Math.min(...ys), Math.max(...ys)] })
    // Into the desk from its near edge (the desk faces straight ahead in this row): every key inside the hands' rest.
    const vs = Object.values(k.centers).map(p => desk.at[2] - p[2])
    const us = Object.values(k.centers).map(p => p[0] - desk.at[0])
    check('every key lies in the near band where the hands rest, inside the desk\'s width', desk.yawDeg === 0 && Math.min(...vs) > 0 && Math.max(...vs) < 0.2 && Math.max(...us.map(Math.abs)) < 0.5, { yawDeg: desk.yawDeg, inM: [+Math.min(...vs).toFixed(3), +Math.max(...vs).toFixed(3)], acrossM: [+Math.min(...us).toFixed(3), +Math.max(...us).toFixed(3)] })
    check('the card stands upright over the desk, clear of the keys', c.home[1] > desk.at[1] + 0.1, { cardY: c.home[1], deskY: desk.at[1] })
    // Look down at the keys, and type with the mouse.
    await page.evaluate(() => window.__xrPages.look({ yawDeg: -8, pitchDeg: -32 }))
    await sleep(500)
    for (const key of ['o', 'k', ' ', 'g', 'o']) await pressKey(page, key)
    check('a press on the desk\'s keys types on the card', (await cardOf(page)).card.text === 'ok go', (await cardOf(page)).card.text)
    await page.screenshot({ path: `${shots}/desk-keys.png` })
    const bot = await botNamed(page, 'atlas')
    await throwTo(page, await roomOnScreen(page, bodyOf(bot)))
    await phaseIs(page, null, 5000)
    await sleep(400)
    check('thrown at a Bot at its seat, it is sent once to that Bot', sends.length === 1 && sends[0].message?.split('\n\n')[0] === 'ok go', sends)
  }

  if (row === 'twice') {
    const bot = await botNamed(page, 'otto')
    const c0 = await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'once only', '')
    await sleep(200)
    const to = await roomOnScreen(page, bodyOf(bot))
    await throwTo(page, to)
    // While it flies and while its send is under way: a second throw, a press where it stood, Enter, more words.
    const home = await roomOnScreen(page, c0.home)
    for (let i = 0; i < 3; i += 1) {
      await page.mouse.move(to.x, to.y); await page.mouse.down(); await page.mouse.move(to.x + 200, to.y - 200, { steps: 3 }); await page.mouse.up()
      await page.mouse.click(home.x, home.y)
      await page.keyboard.press('Enter')
      await say(page, 'again', '').catch(() => {})
    }
    await phaseIs(page, null, 5000)
    await sleep(800)
    check('a second throw, a press, Enter and more words after the throw send nothing more', sends.length === 1 && sends[0].message?.split('\n\n')[0] === 'once only', sends)
    // The same id handed to the write a second time by the page itself: the write does nothing.
    const again = await page.evaluate(async ([id, slot]) => {
      const m = await import('/src/pages/xr/writes.ts')
      return m.cardSend('once only', { bot: 'otto', thread: slot }, id)
    }, [sends[0].key.replace(/^send-/, ''), sends[0].slot])
    await sleep(400)
    check('the write itself, called again with the card\'s id, sends nothing', sends.length === 1 && again === sends[0].slot, { sends: sends.length, sentTo: again })
    // A new card is a new id.
    await cardOut(page)
    await say(page, 'second card', '')
    await sleep(200)
    await throwTo(page, await roomOnScreen(page, bodyOf(await botNamed(page, 'otto'))))
    await phaseIs(page, null, 5000)
    await sleep(500)
    check('a new card has its own id and is sent once', sends.length === 2 && sends[1].key !== sends[0].key && sends[1].message?.split('\n\n')[0] === 'second card', sends.map(s => s.key))
  }

  if (row === 'retry') {
    await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'try me', '')
    await sleep(200)
    await throwTo(page, await openSky(page))
    const back = await waitUntil(async () => { const c = await cardOf(page); return c.card?.failed && c.card.phase === 'out' ? c : null }, 6000, 50)
    check('a send that the gateway refuses brings the card back with its words', !!back && back.card.text === 'try me', back?.card)
    const line = await page.textContent('[data-testid="xr-note"]').catch(() => null)
    check('the room says that it is not sure it went, and that a throw sends the same words', !!line && /Not sure it went/.test(line), line)
    await waitUntil(async () => near((await cardOf(page)).at, back.home, 0.01), 3000, 50)
    await page.screenshot({ path: `${shots}/retry-back.png` })
    await throwTo(page, await openSky(page))
    await phaseIs(page, null, 5000)
    await sleep(600)
    check('thrown again it goes through: two requests with one id, and one new chat', sends.length === 2 && sends[0].key === sends[1].key && slots.length === 1 && sends[1].slot === sends[0].slot, { sends, slots: slots.length })
  }

  if (row === 'away') {
    const c0 = await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'stay here', '')
    await sleep(200)
    // A press on the card that does not leave it, and one that comes back to it: the card stays.
    const p = await roomOnScreen(page, c0.home)
    await page.mouse.move(p.x, p.y); await page.mouse.down(); await sleep(300); await page.mouse.up()
    await sleep(300)
    check('a press on the card that does not move throws nothing', (await cardOf(page)).card.phase === 'out' && sends.length === 0, (await cardOf(page)).card.phase)
    await page.mouse.move(p.x, p.y); await page.mouse.down(); await page.mouse.move(p.x + 500, p.y + 200, { steps: 8 }); await sleep(200)
    check('carried away it is held', (await cardOf(page)).card.phase === 'held', (await cardOf(page)).card.phase)
    await page.mouse.move(p.x + 10, p.y + 5, { steps: 8 }); await sleep(200); await page.mouse.up()
    await sleep(500)
    const c = await cardOf(page)
    check('brought back to its place and let go, it stays with its words', c.card.phase === 'out' && c.card.text === 'stay here' && sends.length === 0, c.card)
    await waitUntil(async () => near((await cardOf(page)).at, c0.home, 0.01), 3000, 50)
    check('it stands at its place again', near((await cardOf(page)).at, c0.home, 0.01), (await cardOf(page)).at)
  }

  if (row === 'nospeech') {
    const c = await cardOut(page)
    check('where the browser has no recognizer the card says so', c.card.mic === 'none' && c.speech === false, { mic: c.card.mic, speech: c.speech })
    check('the keyboard comes at once', !!c.keys, c.keys && { flat: c.keys.flat })
    await page.keyboard.type('typed it')
    await sleep(200)
    check('typed words show on the card', (await cardOf(page)).card.text === 'typed it', (await cardOf(page)).card.text)
    await page.screenshot({ path: `${shots}/nospeech.png` })
    // No target yet: Enter sends nothing; the throw sends.
    await page.keyboard.press('Enter')
    await sleep(300)
    check('Enter sends nothing while the card has no target', sends.length === 0, sends.length)
    await throwTo(page, await openSky(page))
    await phaseIs(page, null, 5000)
    await sleep(500)
    check('thrown into the open it starts one new chat with the typed words', slots.length === 1 && sends.length === 1 && sends[0].message?.split('\n\n')[0] === 'typed it', sends)
  }

  if (row === 'realmic') {
    // Chromium's own recognizer on a host with no microphone: what the card does.
    const c = await cardOut(page)
    await sleep(9500)
    const later = await cardOf(page)
    note('the card with Chromium\'s own recognizer, at once and after 9.5 s', { first: { mic: c.card.mic, keys: !!c.keys }, later: { mic: later.card?.mic, keys: !!later.keys }, speech: c.speech })
    check('with no microphone the recognizer of Chromium never starts: after 8 s the card says that there is no speech here, and its keyboard comes', later.card?.mic === 'none' && !!later.keys, { mic: later.card?.mic, keys: !!later.keys })
    await page.screenshot({ path: `${shots}/realmic.png` })
  }

  if (row === 'real') {
    // The one row whose writes reach the dev gateway: a new chat, which talks to no Bot.
    await cardOut(page)
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'ping from the new chat card', '')
    await sleep(200)
    await throwTo(page, await openSky(page))
    await phaseIs(page, null, 15000)
    await sleep(1500)
    const end = await cardOf(page)
    check('the dev gateway started one new chat and took one message', slots.length === 1 && sends.length === 1 && end.sent === 1 && !!end.slot, { slots: slots.length, sends, slot: end.slot })
    const r = await read(page)
    const paths = Object.values(r.sources).map(s => s.path)
    // The panel is opened at /chat?sid=<key>; once the chat page has loaded it writes its own address anew, from the
    // session's title (the card's words): either address names this chat and no other.
    check('that chat\'s page opens on a panel', paths.some(p => p === `/chat?sid=${end.slot}` || p === '/chat/ping-from-the-new-chat-card'), paths)
    const said = await page.evaluate(async slot => { const j = await fetch('/api/chat/slots/' + encodeURIComponent(slot) + '?limit=20').then(x => x.json()).catch(e => ({ error: String(e) })); return JSON.stringify(j).slice(0, 4000) }, end.slot)
    check('the gateway\'s own record of that chat holds the card\'s words', said.includes('ping from the new chat card'), said.slice(0, 300))
    await sleep(6000)
    await page.screenshot({ path: `${shots}/real-page.png` })
  }

  if (row === 'aim70') {
    // M70 (D104 1), in a session: the Bot nearest the card's place, its width and place read from the build. The aim
    // at 1, 2, 3 and 4 degrees from the place toward that Bot's middle, 1.4 degrees sideways, and at its middle. Each
    // either sends (the message caught in the browser) or puts back, by the rule told in words: the Bot takes the card
    // only when the aim is nearer the Bot's middle than the place's middle. What the card says while held is read too.
    const EYE = 1.6; const e = [0, EYE, 0]
    const look = (yawDeg, pitchDeg = 0) => emu.setHead(page, { position: e, yawDeg: -yawDeg, pitchDeg })
    const lookDir = async d => { await look((Math.atan2(d[0], -d[2]) * 180) / Math.PI, (Math.atan2(d[1], Math.hypot(d[0], d[2])) * 180) / Math.PI); await emu.frames(page, 6) }
    const unit = v => { const l = Math.hypot(...v); return v.map(x => x / l) }
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
    const deg = (a, b) => (Math.acos(Math.max(-1, Math.min(1, dot(unit(a), unit(b))))) * 180) / Math.PI
    const park = async () => { for (const hand of ['left', 'right']) await emu.setRay(page, { hand, origin: [hand === 'left' ? -0.3 : 0.3, 1, 0.2], direction: [hand === 'left' ? -0.577 : 0.577, -0.577, 0.577] }); await emu.frames(page, 3) }
    const pinchOf = async () => (await read(page)).pinch
    const down = async hand => { const before = JSON.stringify((await pinchOf())?.last ?? null); await emu.select(page, { hand, down: true }); await waitUntil(async () => JSON.stringify((await pinchOf())?.last ?? null) !== before, 3000, 15) }
    const up = async hand => { await emu.select(page, { hand, down: false }); await waitUntil(async () => (await pinchOf())?.last?.up !== '', 2000, 15); await emu.frames(page, 2) }
    await page.evaluate(() => window.__xrPages.stroll?.(false))
    await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, 40.7)
    await emu.setInputMode(page, 'hand')
    await look(0, 0)
    await page.click('[data-testid="xr-enter"]')
    await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
    await wholeWindow(page)
    await park(); await emu.frames(page, 8)
    // A Bot that was sent a card walks off and does not come back soon (w1116: Mira 16 degrees away for the rest of the
    // row). After each send the room is loaded again, so each case meets the Bots where they stand at the start.
    const reenter = async () => {
      await page.reload()
      await waitUntil(async () => { try { const cr = (await read(page)).crew; return cr.length > 0 && cr.every(b => b.drawn) } catch { return false } }, 60000, 300)
      await page.evaluate(() => { window.__xrPages.clear(); window.__xrPages.stroll?.(false) })
      await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, 40.7)
      await emu.setInputMode(page, 'hand'); await look(0, 0)
      await page.click('[data-testid="xr-enter"]')
      await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
      await wholeWindow(page); await park(); await emu.frames(page, 8)
    }
    const bring = async words => {
      await page.evaluate(() => document.querySelector('[data-testid="xr-new-chat"]').click())
      const c = await waitUntil(async () => { await emu.frames(page, 2); const k = await cardOf(page); return k.card && near(k.at, k.home, 0.01) ? k : null }, 5000, 50)
      await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
      await say(page, words, ''); await emu.frames(page, 4)
      return c
    }
    let c = await bring('aim case')
    const home = c.home; const h = unit(home.map((v, i) => v - e[i]))
    const crew = (await read(page)).crew.filter(b => !b.seat)
    const near9 = crew.map(b => ({ b, d: deg(bodyOf(b).map((v, i) => v - e[i]), h) })).sort((p, q) => p.d - q.d)
    const nb = near9[0].b; const mid = bodyOf(nb); const j = unit(mid.map((v, i) => v - e[i]))
    const D = deg(j, h); const distM = Math.hypot(...mid.map((v, i) => v - e[i]))
    note('the Bot nearest the card\'s place, as the build has it', { slug: nb.slug, fromPlaceDeg: +D.toFixed(2), distM: +distM.toFixed(3), widthM: nb.widthM, halfWidthDeg: +((Math.atan(nb.widthM / 2 / distM) * 180) / Math.PI).toFixed(2), others: near9.slice(1, 4).map(x => `${x.b.slug}:${x.d.toFixed(1)}`) })
    // Each aim is made from the card's place AT THAT CASE: a new card's place follows his head when it comes out
    // (w1112: the aside case was reckoned from the first card's place and went to Mira).
    const aimOf = (hh, jj, kind, x) => {
      const DD = deg(jj, hh)
      if (kind === 'along') { const a = (DD * Math.PI) / 180, t = ((x === 'mid' ? DD : x) * Math.PI) / 180; return hh.map((v, i) => (Math.sin(a - t) / Math.sin(a)) * v + (Math.sin(t) / Math.sin(a)) * jj[i]) }
      const side = unit(cross(hh, jj)); const t = (x * Math.PI) / 180; return hh.map((v, i) => Math.cos(t) * v + Math.sin(t) * side[i])
    }
    const cases = [['1 deg toward', 'along', 1], ['2 deg toward', 'along', 2], ['3 deg toward', 'along', 3], ['4 deg toward', 'along', 4], ['1.4 deg aside', 'aside', 1.4], ['at its middle', 'along', 'mid']]
    for (const [label, kind, x] of cases) {
      if (!c || (await cardOf(page)).card === null) c = await bring('aim case')
      const kh = await cardOf(page)
      const h = unit(kh.home.map((v, i) => v - e[i]))
      // A Bot sent a card walks off and comes back: wait (10 s at most) for one within 9 degrees of the place.
      await waitUntil(async () => { await emu.frames(page, 2); return (await read(page)).crew.some(x => !x.seat && deg(bodyOf(x).map((v, i) => v - e[i]), h) < 9) }, 10000, 250)
      // The Bot nearest the card's place NOW: a Bot that was sent a card may walk off (w1114: Mira 17 degrees away by the
      // third case, so the 3 and 4 degree cases judged nothing near the line).
      const nbNow = (await read(page)).crew.filter(x => !x.seat).map(x => ({ x, d: deg(bodyOf(x).map((v, i) => v - e[i]), h) })).sort((p, q) => p.d - q.d)[0].x
      const j = unit(bodyOf(nbNow).map((v, i) => v - e[i]))
      const dir = aimOf(h, j, kind, x)
      const sends0 = sends.length
      const toMid = deg(dir, j), toPlace = deg(dir, h)
      const want = toMid < toPlace ? 'send' : 'back'
      // The take: the dot on the card's place, a pinch of the resting left hand. A fresh card right after a send can
      // meet the quiet time that follows a throw (w1111: the pinch took nothing): a few tries, each noted.
      let held0 = null
      for (let t = 0; t < 4 && held0 !== 'held'; t += 1) {
        const kk = await cardOf(page)
        await lookDir(unit(kk.home.map((v, i) => v - e[i]))); await down('left'); await emu.frames(page, 3)
        held0 = (await cardOf(page)).card?.phase
        if (held0 !== 'held') { note(`the take, try ${t + 1}, took nothing`, { phase: held0, last: (await pinchOf())?.last, on: (await pinchOf())?.head?.on?.what }); await up('left'); await sleep(700); await emu.frames(page, 6) }
      }
      await lookDir(dir); await sleep(300); await emu.frames(page, 4)
      const k = await cardOf(page)
      await page.screenshot({ path: `${shots}/aim70-${label.replace(/ /g, '-')}.png` })
      await up('left')
      await waitUntil(async () => { await emu.frames(page, 3); const x = (await cardOf(page)).card; return x === null || (x.phase === 'out' && near((await cardOf(page)).at, home, 0.01)) }, 6000, 50)
      await sleep(400); await emu.frames(page, 4)
      const after = await cardOf(page)
      const did = sends.length > sends0 ? 'send' : after.card?.phase === 'out' ? 'back' : 'other'
      const sentTo = sends.length > sends0 ? sends.at(-1) : null
      // The build's own reading AT the let-go (`letGo`): what the card said then and what it acted on. If it said
      // "Back" and sent, the build broke M77; if it said "To <Bot>" and sent, the row read the card too early.
      const lg = after.letGo ?? null
      note(`${label}: at the let-go`, { rowRead: { aim: k.aim ?? null, top: k.top ?? null }, letGo: lg, did, buildAgrees: !!lg && (did === 'send') === (lg.target !== null) && /back/i.test(lg.top ?? '') === (lg.target === null) })
      if (sentTo) { await reenter(); c = null }
      check(`${label} (${toPlace.toFixed(2)} from the place, the Bot ${deg(j, h).toFixed(2)} from it, ${toMid.toFixed(2)} from ${nbNow.slug}'s middle): the rule says ${want}; held, the card aims ${JSON.stringify(k.aim ?? null)} and says "${k.top}"; let go it did ${did}`,
        held0 === 'held' && (x === 'mid' || deg(j, h) < 9) && did === want && (want === 'send' ? k.aim?.to === 'bot' && k.aim.slug === nbNow.slug && sentTo?.message?.startsWith('aim case') : k.aim === null && /back/i.test(k.top ?? '')),
        { held0, aim: k.aim ?? null, top: k.top ?? null, did, sent: sentTo && { slot: sentTo.slot, message: sentTo.message?.split('\n\n')[0] } })
    }
    // M77 (D106 4): the aim rests on the nearest Bot's middle for 200 ms: sent; for under 100 ms: back (a send needs
    // the same target for 150 ms); and an aim that crosses to the place in the last two frames: the act is what the
    // card said just before the let-go. The rest is timed by the page's own clock, from the head's set to the up.
    // M81 (REVIEW-43 3.3 1): the rests are far from 150 ms, 400 and 60, and judged by the row's own clock as well.
    for (const [label, restMs, cross] of [['rests 400 ms on the Bot', 400, false], ['rests 60 ms on the Bot', 60, false], ['crosses to the place in the last two frames', 300, true]]) {
      if (!(await cardOf(page)).card) c = await bring('aim case')
      const kh = await cardOf(page)
      const h = unit(kh.home.map((v, i) => v - e[i]))
      const bot = (await read(page)).crew.filter(x => !x.seat).map(x => ({ x, d: deg(bodyOf(x).map((v, i) => v - e[i]), h) })).sort((p, q) => p.d - q.d)[0].x
      const mid = unit(bodyOf(bot).map((v, i) => v - e[i]))
      const sends0 = sends.length
      await lookDir(h); await down('left'); await emu.frames(page, 3)
      // The rest by the page's own clock (D114): the clock stops BEFORE the head moves onto the Bot, then goes on
      // exactly `restMs` (its frames with it), and the up goes in the next frames of that same clock. The row's sleep
      // is no clock of the page's (w1140: 703 ms), and a head set while the clock runs rests before the count.
      const stepped = !cross && restMs > 0
      if (stepped) await page.clock.pauseAt(await page.evaluate(() => Date.now()) + 250)
      const t0 = await page.evaluate(() => performance.now())
      await look((Math.atan2(mid[0], -mid[2]) * 180) / Math.PI, (Math.atan2(mid[1], Math.hypot(mid[0], mid[2])) * 180) / Math.PI)
      if (stepped) await page.clock.runFor(restMs)
      else { await emu.frames(page, 1); if (restMs) { await sleep(restMs); await emu.frames(page, 2) } }
      if (cross) { await look((Math.atan2(h[0], -h[2]) * 180) / Math.PI, (Math.atan2(h[1], Math.hypot(h[0], h[2])) * 180) / Math.PI); await emu.frames(page, 2) }
      const said = await cardOf(page)
      // The up goes in with the next frame at once: `up` waits on the probe with no frame, and the page took the up
      // about 590 ms late (w1129: a 60 ms rest stood 651 ms by this clock and sent).
      if (stepped) { await emu.select(page, { hand: 'left', down: false }); await page.clock.runFor(34); await page.clock.resume() }
      else { await emu.select(page, { hand: 'left', down: false }); await emu.frames(page, 1) }
      await waitUntil(async () => { await emu.frames(page, 1); return (await pinchOf())?.last?.up !== '' }, 2000, 0)
      // The rest by the row's clock ends when the page has the up, not when the row asks for it.
      const t1 = await page.evaluate(() => performance.now())
      await waitUntil(async () => { await emu.frames(page, 3); const x = (await cardOf(page)).card; return x === null || x.phase === 'out' }, 6000, 50)
      await sleep(400)
      const did = sends.length > sends0 ? 'send' : 'back'
      const restedMs = Math.round(t1 - t0)
      // What the card said AT the let-go and what it did with it, from the build's one reading (`letGo`).
      const at = (await cardOf(page)).letGo ?? null
      const saidBack = /back/i.test(at?.top ?? '')
      const same = !!at && (did === 'send') === (at.target !== null) && saidBack === (at.target === null)
      // Timing, by the build's own clock at the let-go (`stoodMs`: how long the shown target's reading had stood): a
      // send only past 150 ms, and a target that stood 150 ms is sent. The row's own clock is not the let-go's: the up
      // reaches the page some frames after the row asks for it (w1116: 113 ms by the row, more by the page).
      const timed = !at ? false : did === 'send' ? at.stoodMs >= 150 : !(at.target && at.stoodMs >= 150)
      // By the row's own clock too: the 400 ms rest sends to that Bot; the 60 ms rest goes back, and at the let-go the
      // raw target was that Bot, standing under 150 ms (S10: the put-back is judged by the raw target's time); the
      // crossing goes back with the place as the raw target.
      // A rest that the row's clock puts between 150 and 300 ms is judged by the build's clock alone; the case says so.
      // With the page's clock stopped and stepped, the rest IS `restMs` on the page's clock (the row's count then also
      // holds the build's own wait for an opened pinch, about 110 ms, w1168): judge by it.
      const restBy = stepped ? restMs : restedMs
      const byRow = cross ? did === 'back' && (at?.raw ?? null) === null
        : restBy >= 300 ? did === 'send' && at?.target?.slug === bot.slug
          : restBy < 150 ? did === 'back' && at?.raw?.slug === bot.slug && at.stoodMs < 150
            : restMs < 150 ? false : true
      check(`M77: the aim ${label} (${restedMs} ms by the row's clock): at the let-go the raw target ${JSON.stringify(at?.raw ?? null)} had stood ${at?.stoodMs ?? '?'} ms, the card said "${at?.top ?? '?'}", and it did ${did}`, same && timed && byRow, { bot: bot.slug, rowMs: restedMs, before: { aim: said.aim, top: said.top }, atLetGo: at, did })
      if (did === 'send') { await reenter(); c = null }
    }
    // D105 3, by pixels: held over Home's strip, the card's top line is drawn over Home. The card's top band is read
    // in two frames at one head pose: held there (A), and with the card back at its place (C, Home alone). Home's own
    // letters are the bright pixels of C in that band; the share of them that are the same in A is near 1 when Home is
    // drawn over the card (the old build), and near 0 when the card is over Home. Then, let go, Home's numbers are
    // whole: the same band in a third frame after the let-go matches C.
    {
      c = (await cardOf(page)).card ? c : await bring('aim case')
      const kh = await cardOf(page)
      const h = unit(kh.home.map((v, i) => v - e[i]))
      const strip = (await read(page)).home
      // The aim that puts the held card's top line on the strip, by the build's numbers (`chatCard.ts` CARD): held 0.5 m
      // off, 12 degrees over the aim, 0.075 m tall at 60 %, so its top band is 14.6 to 16.3 degrees over the aim. The
      // aim is 15.4 degrees under the strip's middle, at its yaw.
      const sc = strip?.center
      const sd = sc ? unit(sc.map((v, i) => v - e[i])) : h
      const sYaw = Math.atan2(sd[0], -sd[2]), sPitch = Math.asin(sd[1]) - (15.4 * Math.PI) / 180
      const aimDir = [Math.sin(sYaw) * Math.cos(sPitch), Math.sin(sPitch), -Math.cos(sYaw) * Math.cos(sPitch)]
      const homeDeg = deg(aimDir, h)
      await lookDir(h); await down('left'); await emu.frames(page, 3)
      await lookDir(aimDir); await sleep(400); await emu.frames(page, 4)
      const held = await cardOf(page)
      const A = (await page.screenshot({ path: `${shots}/aim70-over-home-held.png` })).toString('base64')
      // The card's top band on the screen: its middle `at`, 0.12 x 0.075 m held (60 %), facing the eye.
      const vp = page.viewportSize(); const k = Math.tan((40.7 * Math.PI) / 360)
      const fwd = aimDir; const right = unit(cross(fwd, [0, 1, 0])); const upv = cross(right, fwd)
      const toScreen = p => { const d = p.map((v, i) => v - e[i]); const z = dot(d, fwd); return { x: vp.width / 2 + (dot(d, right) / z / (k * vp.width / vp.height)) * (vp.width / 2), y: vp.height / 2 - (dot(d, upv) / z / k) * (vp.height / 2) } }
      const at = held.at
      const corner = (sx, sy) => toScreen([0, 1, 2].map(i => at[i] + right[i] * sx * 0.06 + upv[i] * sy * 0.0375))
      const tl = corner(-1, 1), br = corner(1, 0.6)
      const band = { x0: Math.round(Math.min(tl.x, br.x)), y0: Math.round(Math.min(tl.y, br.y)), x1: Math.round(Math.max(tl.x, br.x)), y1: Math.round(Math.max(tl.y, br.y)) }
      await up('left')
      await waitUntil(async () => { await emu.frames(page, 3); const x = (await cardOf(page)).card; return x === null || x.phase === 'out' }, 6000, 50)
      await sleep(500); await emu.frames(page, 6)
      const C = (await page.screenshot({ path: `${shots}/aim70-over-home-after.png` })).toString('base64')
      const same = await page.evaluate(async ([a, cc, b]) => {
        const load = async s => { const img = await new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = `data:image/png;base64,${s}` }); const cv = document.createElement('canvas'); cv.width = img.naturalWidth; cv.height = img.naturalHeight; const x = cv.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0); return x }
        const xa = await load(a), xc = await load(cc)
        const w = Math.max(1, b.x1 - b.x0), hh = Math.max(1, b.y1 - b.y0)
        const da = xa.getImageData(b.x0, b.y0, w, hh).data, dc = xc.getImageData(b.x0, b.y0, w, hh).data
        let bright = 0, kept = 0
        for (let i = 0; i < dc.length; i += 4) {
          if ((dc[i] + dc[i + 1] + dc[i + 2]) / 3 < 150) continue
          bright += 1
          if ((Math.abs(da[i] - dc[i]) + Math.abs(da[i + 1] - dc[i + 1]) + Math.abs(da[i + 2] - dc[i + 2])) / 3 < 30) kept += 1
        }
        return { bright, keptShare: bright ? +(kept / bright).toFixed(3) : null }
      }, [A, C, band])
      check('held over Home\'s strip, the card\'s top line is drawn over it: of Home\'s own bright pixels in that band, under 10 % show through (D105 3)', held.card?.phase === 'held' && same.bright > 50 && same.keptShare !== null && same.keptShare < 0.1, { aimFromPlaceDeg: +homeDeg.toFixed(2), top: held.top, band, ...same, stripAt: sc })
      note('after the let-go Home is whole again: the band of the last frame is Home alone (the still aim70-over-home-after.png)', { phase: (await cardOf(page)).card?.phase ?? null })
    }
    // M70's case beside: Bots walk while he aims near the place. Each 300 ms the card's shown aim is held against the
    // rule worked from where the Bots stand then (a sample within 0.3 degrees of the halfway line is not judged), and
    // the let-go does what the card showed last.
    {
      if (!(await cardOf(page)).card) c = await bring('aim case')
      const kh = await cardOf(page)
      const h = unit(kh.home.map((v, i) => v - e[i]))
      const rule = (dir, crewNow) => {
        const toPlace = deg(dir, h)
        let best = null, bestOff = Infinity, mid = null, midOff = Infinity, edge = Infinity
        for (const b of crewNow.filter(x => !x.seat)) {
          const m = bodyOf(b).map((v, i) => v - e[i]); const dist = Math.hypot(...m)
          const toMid = deg(dir, m); const off = toMid - (Math.atan(b.widthM / 2 / dist) * 180) / Math.PI
          if (off < bestOff) { best = b; bestOff = off }
          if (off <= 4 && toMid < midOff) { mid = b; midOff = toMid }
        }
        if (toPlace < 9) { edge = Math.abs(midOff - toPlace); return { aim: mid && midOff < toPlace ? mid.slug : null, edge } }
        return { aim: best && bestOff <= 4 ? best.slug : 'open', edge: Math.abs(bestOff - 4) }
      }
      const near9b = (await read(page)).crew.filter(b => !b.seat).map(b => ({ b, d: deg(bodyOf(b).map((v, i) => v - e[i]), h) })).sort((p, q) => p.d - q.d)[0].b
      const jj = unit(bodyOf(near9b).map((v, i) => v - e[i])); const DD = deg(jj, h)
      // A third of the way from the place: back by the rule while the Bots stand, so a walking Bot is what changes it.
      const t = ((DD * 0.35) * Math.PI) / 180, a = (DD * Math.PI) / 180
      const dir = h.map((v, i) => (Math.sin(a - t) / Math.sin(a)) * v + (Math.sin(t) / Math.sin(a)) * jj[i])
      await lookDir(h); await down('left'); await emu.frames(page, 3)
      await page.evaluate(() => window.__xrPages.stroll?.(true, 300))
      await lookDir(dir)
      const samples = []
      for (let s = 0; s < 10; s += 1) {
        await sleep(300); await emu.frames(page, 3)
        const k = await cardOf(page); const r = rule(dir, (await read(page)).crew)
        const shown = k.aim?.to === 'bot' ? k.aim.slug : k.aim?.to === 'open' ? 'open' : null
        samples.push({ shown, rule: r.aim, edge: +r.edge.toFixed(2), judged: r.edge >= 0.3 })
      }
      const lastShown = samples.at(-1).shown
      const sends0 = sends.length
      await up('left')
      await waitUntil(async () => { await emu.frames(page, 3); const x = (await cardOf(page)).card; return x === null || x.phase === 'out' }, 6000, 50)
      await sleep(500)
      await page.evaluate(() => window.__xrPages.stroll?.(false))
      const did = sends.length > sends0 ? `send:${sends.at(-1).slot}` : 'back'
      const judged = samples.filter(x => x.judged)
      check('Bots walking while he aims near the place: each judged sample the card shows what the rule says, and the let-go does what it showed last', judged.length > 0 && judged.every(x => x.shown === x.rule) && (lastShown === null ? did === 'back' : did.startsWith('send')), { samples, lastShown, did })
    }
  }

  if (row === 'session' || row === 'palm' || row === 'enterlong') {
    const AURA_FOVY = 40.7
    const EYE = 1.6
    const look = (yawDeg, pitchDeg = 0) => emu.setHead(page, { position: [0, EYE, 0], yawDeg: -yawDeg, pitchDeg })
    const dotAt = async at => { const d = at.map((v, i) => v - [0, EYE, 0][i]); await look((Math.atan2(d[0], -d[2]) * 180) / Math.PI, (Math.atan2(d[1], Math.hypot(d[0], d[2])) * 180) / Math.PI); await emu.frames(page, 4) }
    const park = async () => { for (const hand of ['left', 'right']) await emu.setRay(page, { hand, origin: [hand === 'left' ? -0.3 : 0.3, 1, 0.2], direction: [hand === 'left' ? -0.577 : 0.577, -0.577, 0.577] }); await emu.frames(page, 3) }
    const pinchOf = async () => (await read(page)).pinch
    const down = async hand => { const before = JSON.stringify((await pinchOf())?.last ?? null); await emu.select(page, { hand, down: true }); await waitUntil(async () => JSON.stringify((await pinchOf())?.last ?? null) !== before, 3000, 15) }
    const up = async hand => { await emu.select(page, { hand, down: false }); await waitUntil(async () => (await pinchOf())?.last?.up !== '', 2000, 15); await emu.frames(page, 2) }
    await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, AURA_FOVY)
    await emu.setInputMode(page, 'hand')
    await look(0, 0)
    await page.click('[data-testid="xr-enter"]')
    await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
    await wholeWindow(page)
    await park()
    await emu.frames(page, 8)
    if (row === 'palm') {
      // The left palm, open and turned to the face, shows the palette; the dot on its "new chat", a pinch of the right hand.
      await emu.setHandPose(page, { hand: 'left', position: [-0.12, 1.42, -0.4], palmTowardHead: true })
      const pal = await waitUntil(async () => { await emu.frames(page, 3); const p = (await read(page)).palette; return p?.side && p.newChat ? p : null }, 6000, 50)
      check('the left palm shows the palette, with a "new chat" button over its tiles', !!pal && pal.side === 'left', pal && { side: pal.side, newChat: pal.newChat })
      await page.screenshot({ path: `${shots}/palm.png` })
      await dotAt(pal.newChat)
      const lit = (await pinchOf())?.head?.on?.what
      note('what the dot lies on', lit)
      await down('right'); await emu.frames(page, 3); await up('right')
      const c = await waitUntil(async () => { await emu.frames(page, 2); const k = await cardOf(page); return k.card ? k : null }, 4000, 50)
      check('a pinch with the dot on it brings the card out', !!c && c.card.phase === 'out', { card: c?.card, last: (await pinchOf())?.last })
      await emu.setHandPose(page, { hand: 'left', clear: true })
      await park()
      await page.screenshot({ path: `${shots}/palm-card.png` })
    } else {
      // The bar is not in a session: the card is brought out by the page's own hook of the button, as the palm's tile does.
      await page.evaluate(() => document.querySelector('[data-testid="xr-new-chat"]').click())
    }
    const c = await waitUntil(async () => { await emu.frames(page, 2); const k = await cardOf(page); return k.card && near(k.at, k.home, 0.01) ? k : null }, 5000, 50)
    check('in a session the card stands at its place', !!c, c && { at: c.at, home: c.home })
    await waitUntil(async () => ((await cardOf(page)).card?.mic === 'on'), 3000, 40)
    await say(page, 'from the glasses', '')
    await emu.frames(page, 4)
    // The dot on the card, a pinch of the resting left hand takes it; the head turns to a Bot; the fingers open.
    await dotAt(c.home)
    note('the dot lies on', (await pinchOf())?.head?.on?.what)
    await down('left')
    await emu.frames(page, 3)
    check('a pinch of a resting hand with the dot on the card takes it', (await cardOf(page)).card.phase === 'held', { phase: (await cardOf(page)).card.phase, last: (await pinchOf())?.last, hands: (await pinchOf())?.hands })
    const bot = await botNamed(page, 'mira')
    await dotAt(bodyOf(bot))
    await sleep(700)
    await emu.frames(page, 4)
    await page.screenshot({ path: `${shots}/session-held.png` })
    {
      // How far the Bot's body is from the card's own place, at his eye (the card puts itself back under 9 degrees).
      const k = await cardOf(page); const b = bodyOf(bot); const e = [0, EYE, 0]
      const ang = (p, q) => { const a = p.map((v, i) => v - e[i]); const c = q.map((v, i) => v - e[i]); const d = Math.hypot(...a) * Math.hypot(...c); return +((Math.acos(Math.max(-1, Math.min(1, (a[0] * c[0] + a[1] * c[1] + a[2] * c[2]) / d))) * 180) / Math.PI).toFixed(1) }
      note('the card while held over the Bot', { card: k.card, grip: k.grip ?? 'no probe', at: k.at, home: k.home, bot: bot.slug, body: b, bodyFromHomeDeg: k.home ? ang(b, k.home) : null })
    }
    await up('left')
    const last = (await pinchOf())?.last
    note('what the pinch was, by the route', last)
    // What the card did with the let-go, by the wall clock: its phase and target at once, and 1 s later.
    note('the card at the let-go', (await cardOf(page)).card)
    await sleep(1000); await emu.frames(page, 3)
    note('the card 1 s after the let-go', { card: (await cardOf(page)).card, sends: sends.length, chats: slots.length })
    await waitUntil(async () => { await emu.frames(page, 3); return (await cardOf(page)).card === null }, 6000, 50)
    await emu.frames(page, 6)
    check('held longer than half a second with the hand still (a long press by the route), the let-go still throws', last?.kind === 'long' && sends.length === 1, { kind: last?.kind, ms: last?.ms })
    check('the head turned to a Bot and the fingers opened: one send to that Bot', sends.length === 1 && sends[0].message?.split('\n\n')[0] === 'from the glasses' && slots.length === 0, sends)
    check('no hand was raised: both rest under the line', Object.values((await pinchOf())?.hands ?? {}).every(h => h.raised === false), (await pinchOf())?.hands)
    if (row === 'session' || row === 'enterlong') {
      // A second card, thrown with no words: the keyboard stands in front, and the dot with a pinch types.
      await look(0, 0)
      await emu.frames(page, 4)
      await page.evaluate(() => document.querySelector('[data-testid="xr-new-chat"]').click())
      const c2 = await waitUntil(async () => { await emu.frames(page, 2); const k = await cardOf(page); return k.card && near(k.at, k.home, 0.01) ? k : null }, 5000, 50)
      await dotAt(c2.home)
      await down('left'); await emu.frames(page, 3)
      // Into the open: over the Bots' heads (20 degrees aside and 12 up found Mate's desk in the room of places).
      await look(20, 30); await emu.frames(page, 6)
      await up('left')
      const t = await waitUntil(async () => { await emu.frames(page, 2); const k = await cardOf(page); return k.card?.phase === 'typing' && k.keys ? k : null }, 4000, 50)
      check('an empty card thrown into the open brings the keyboard, standing in front', !!t && t.keys.flat === false && t.card.target?.to === 'open' && sends.length === 1, t && { target: t.card.target, flat: t.keys.flat })
      for (const key of ['h', 'i']) {
        await dotAt((await cardOf(page)).keys.centers[key])
        await down('right'); await emu.frames(page, 3); await up('right')
      }
      check('the dot with a pinch types on the standing keyboard', (await cardOf(page)).card.text === 'hi', { text: (await cardOf(page)).card.text, on: (await pinchOf())?.head?.on?.what, last: (await pinchOf())?.last })
      await page.screenshot({ path: `${shots}/session-keys.png` })
      if (row === 'enterlong') {
        // M47 (D91 1), its worst case: Enter with words typed, a pinch held 2 s by the clock, the head turned 20
        // degrees away at 1.2 s: nothing is sent. Then the same with the head still on Enter: one send. The sends are
        // answered in the browser.
        const hold = async away => {
          await dotAt((await cardOf(page)).keys.centers.Enter)
          const at0 = { yaw: null }
          await down('right'); const t0 = Date.now(); let turned = false
          while (Date.now() - t0 < 2000) { await emu.frames(page, 1); if (away && !turned && Date.now() - t0 > 1200) { turned = true; const d = (await cardOf(page)).keys.centers.Enter.map((v, i) => v - [0, EYE, 0][i]); await look((Math.atan2(d[0], -d[2]) * 180) / Math.PI + 20, (Math.atan2(d[1], Math.hypot(d[0], d[2])) * 180) / Math.PI) } await sleep(5) }
          await up('right'); await emu.frames(page, 6); await sleep(800)
          return { kind: (await pinchOf())?.last?.kind ?? null, sends: sends.length, card: (await cardOf(page)).card?.phase ?? null, text: (await cardOf(page)).card?.text ?? null, at0 }
        }
        const a = await hold(true)
        check('Enter with words typed, a 2 s pinch, the head turned 20 degrees away before the release: nothing is sent, the words stay', a.kind === 'long' && a.sends === 1 && a.text === 'hi', a)
        const b = await hold(false)
        await waitUntil(async () => { await emu.frames(page, 3); return (await cardOf(page)).card === null }, 6000, 50)
        check('the same with the head still on Enter: one send, with the words', b.kind === 'long' && sends.length === 2 && sends[1]?.message?.split('\n\n')[0] === 'hi', { b, sends })
      } else {
      await dotAt((await cardOf(page)).keys.centers.Enter)
      await down('right'); await emu.frames(page, 3); await up('right')
      await waitUntil(async () => { await emu.frames(page, 3); return (await cardOf(page)).card === null }, 6000, 50)
      check('Enter by the dot and a pinch sends it into a new chat, once', sends.length === 2 && slots.length === 1 && sends[1].message?.split('\n\n')[0] === 'hi', { sends, slots: slots.length })
      }
    }
  }
  if (row === 'touch') {
    // desk=1 in an emulated session: the desk's "new chat" tile and the keys on the desk, by a finger's touch
    // (the right hand in the emulator's 'point' shape, its index tip down onto the desk and up again).
    const POINT = { palm: [0, -0.6, 1], fingers: [0, -1, -0.6] }
    const park = async () => { for (const hand of ['left', 'right']) await emu.setRay(page, { hand, origin: [hand === 'left' ? -0.3 : 0.3, 1, 0.2], direction: [hand === 'left' ? -0.577 : 0.577, -0.577, 0.577] }); await emu.frames(page, 3) }
    let offset = null
    const tipAt = async (spot, deskY, h) => { await emu.setHandPose(page, { hand: 'right', position: [spot[0] - offset[0], deskY + h - offset[1], spot[2] - offset[2]], ...POINT }); await emu.frames(page, 2) }
    const touchOnce = async (spot, deskY) => {
      if (!offset) {
        const w = [spot[0], deskY + 0.15, spot[2] + 0.05]
        await emu.setHandPose(page, { hand: 'right', position: w, ...POINT })
        await emu.frames(page, 4)
        offset = (await read(page)).touch.tip.right.map((v, i) => v - w[i])
      }
      for (const h of [0.06, 0.06, 0.06, 0.06, 0.04, 0.025, 0.015, 0.008]) await tipAt(spot, deskY, h)
      await sleep(150)
      await emu.frames(page, 3)
      for (const h of [0.02, 0.035, 0.05, 0.06]) await tipAt(spot, deskY, h)
      await emu.frames(page, 3)
      return (await read(page)).touch
    }
    await page.evaluate(f => { window.__xrEmuDevice.fovy = (f * Math.PI) / 180; window.__xrEmuDevice.ipd = 0 }, 40.7)
    await emu.setInputMode(page, 'hand')
    await emu.setHead(page, { position: [0, 1.6, 0], yawDeg: 0, pitchDeg: 0 })
    await page.click('[data-testid="xr-enter"]')
    await page.waitForFunction(() => window.__xrPages.read().inSession, null, { timeout: 30000 })
    await wholeWindow(page)
    await park()
    for (const hand of ['left', 'right']) await emu.setHandShape(page, { hand, pose: hand === 'right' ? 'point' : 'default' })
    await sleep(1500)
    await emu.frames(page, 6)
    const r = await read(page)
    const deskY = r.desk.at[1]
    const tile = r.desk.tiles.find(t => t.id === 'card')
    check('in a session the desk has a "new chat" tile, in the place that the start view leaves free', !!tile && r.desk.tiles.length === 4, r.desk.tiles.map(t => `${t.id}: ${t.label}`))
    const t1 = await touchOnce([tile.center[0], deskY, tile.center[2]], deskY)
    const c = await waitUntil(async () => { await emu.frames(page, 2); const k = await cardOf(page); return k.card ? k : null }, 4000, 50)
    check('a finger\'s touch on the tile brings the card out', !!c, { presses: t1.presses, last: t1.last, card: c?.card?.phase })
    check('with no recognizer the keyboard lies on the desk at once', !!c?.keys && c.keys.flat === true, c?.keys && { at: c.keys.at, flat: c.keys.flat })
    const typedBy = []
    for (const key of ['h', 'i']) {
      const at = (await cardOf(page)).keys.centers[key]
      const t = await touchOnce([at[0], deskY, at[2]], deskY)
      typedBy.push({ key, last: t.last, presses: t.presses })
    }
    await emu.frames(page, 4)
    check('a finger\'s touch on a key of the desk types it on the card', (await cardOf(page)).card.text === 'hi', { text: (await cardOf(page)).card.text, typedBy })
    await emu.setHead(page, { position: [0, 1.6, 0], yawDeg: 6, pitchDeg: -40 })
    await emu.frames(page, 6)
    await page.screenshot({ path: `${shots}/touch-keys.png` })
    // Enter by touch sends nothing: the card has no target yet. The throw is the dot's.
    const enterAt = (await cardOf(page)).keys.centers.Enter
    await touchOnce([enterAt[0], deskY, enterAt[2]], deskY)
    await emu.frames(page, 4)
    check('Enter sends nothing while the card has no target', sends.length === 0 && (await cardOf(page)).card.phase === 'out', { sends: sends.length })
  }
  note('page errors', errors.filter(e => !/queryFn|WebSocket connection|status of (403|503)/.test(e)).slice(0, 6))
} catch (e) {
  check('the row ran to its end', false, String(e?.stack ?? e).slice(0, 700))
} finally {
  writeFileSync(`${dir}/card-${row}.json`, JSON.stringify(out, null, 1))
  await got.browser.close().catch(() => {})
}
process.exit(out.checks.every(c => c.ok) ? 0 : 1)
