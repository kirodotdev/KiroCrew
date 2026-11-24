// REVIEW-73 M242: the flat preview's mouse click on an `ask` board at its Bot (`takeBoard` keeps it in this tab and
// sends nothing; the sample crew, `fixture`, as the session rows use). A click held N frames of the page's own frame clock must take it; a press that begins off the board
// (the empty room above it) and is let go on it must not. Each case starts from a page loaded anew.
// usage (under the lock, through rowset8.sh): PREVIEW_CASES=hold1+hold3+hold30+offon node board-preview.mjs
import { open, read, sleep, waitUntil } from './dash3d-lib.mjs'

const cases = (process.env.PREVIEW_CASES ?? 'hold1+hold3+hold30+offon').split('+')
const results = []
let got = await open({ fixture: true })
const frames = n => got.page.evaluate(k => new Promise(done => { let i = 0; const tick = () => { i += 1; if (i >= k) done(); else requestAnimationFrame(tick) }; requestAnimationFrame(tick) }), n)
const screenOf = async ndc => { const c = await got.page.evaluate(() => { const b = document.querySelector('[data-testid="xr-canvas"]').getBoundingClientRect(); return { x: b.x, y: b.y, w: b.width, h: b.height } }); return { x: c.x + ((ndc[0] + 1) / 2) * c.w, y: c.y + ((1 - ndc[1]) / 2) * c.h } }
const boardsNow = async () => ((await read(got.page)).boards?.boards ?? []).map(b => ({ id: b.needId, bot: b.bot, kind: b.kind, where: b.where }))
for (const k of cases) {
  const b = await waitUntil(async () => (await boardsNow()).find(x => x.kind === 'ask' && x.where === 'bot') ?? null, 30000, 500)
  if (!b) { console.log(`NOT-JUDGED ${k}: no ask board at a Bot: ${JSON.stringify(await boardsNow())}`); continue }
  const bot = (await read(got.page)).crew.find(c => c.slug === b.bot)
  await got.page.evaluate(y => window.__xrPages.look({ yawDeg: y, pitchDeg: -12, lens: 'start' }), (Math.atan2(bot.feet[0], -bot.feet[2]) * 180) / Math.PI)
  await sleep(1200)
  const m = await waitUntil(async () => (await read(got.page)).boardMarks?.[b.id]?.ndc?.body ?? null, 5000, 200)
  if (!m) { console.log(`NOT-JUDGED ${k}: the board is not on the screen`); continue }
  const on = await screenOf(m)
  const hold = Number(/^hold(\d+)$/.exec(k)?.[1] ?? 3)
  const from = k === 'offon' ? { x: on.x, y: Math.max(5, on.y - 260) } : on
  await got.page.mouse.move(from.x, from.y, { steps: 2 }); await frames(3)
  await got.page.mouse.down(); await frames(hold)
  if (k === 'offon') { await got.page.mouse.move(on.x, on.y, { steps: 4 }); await frames(3) }
  await got.page.mouse.up(); await frames(4); await sleep(800)
  const after = (await boardsNow()).find(x => x.id === b.id)?.where ?? null
  const r = { case: k, board: `${b.bot}:${b.kind}`, holdFrames: hold, from: [Math.round(from.x), Math.round(from.y)], on: [Math.round(on.x), Math.round(on.y)], before: b.where, after }
  results.push(r)
  console.log(`  preview ${k}: ${JSON.stringify(r)}`)
  // A taken board goes back by the probe's own way out: the page loaded anew, so the next case starts at the Bot.
  if (after !== 'bot') { await got.browser.close(); got = await open({ fixture: true }) }
}
const takes = results.filter(r => r.case.startsWith('hold'))
const offs = results.filter(r => r.case === 'offon')
console.log(`${takes.length >= 1 && takes.every(r => r.after === 'held') ? 'PASS' : 'FAIL'} preview: a mouse click on an ask board at its Bot, held ${takes.map(r => r.holdFrames).join(', ')} frames, takes it (${takes.filter(r => r.after === 'held').length} of ${takes.length})`)
if (offs.length) console.log(`${offs.every(r => r.after === 'bot') ? 'PASS' : 'FAIL'} preview: a press that begins above the board and is let go on it takes nothing (${offs.filter(r => r.after === 'bot').length} of ${offs.length})`)
await got.browser.close()
process.exit(0)
