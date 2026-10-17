/**
 * Screenshots of the Context tab reading pushed `usage` frames instead of polling,
 * via the capture/context-trace-push harness (real tab, real frame handler, a REST
 * stub that counts reads). Every frame asserts the read count it claims before it
 * is shot, so no image can show a number the run did not produce.
 *
 * The page clock is Playwright's, installed before load, so "five minutes pass"
 * is real elapsed time for every timer the page arms -- a leftover poll would fire.
 *
 * Usage: node scripts/capture-context-trace-push.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/context-trace-push'
const COALESCE_MS = 250

const b = await chromium.launch()

async function open(theme) {
  const p = await (await b.newContext({ viewport: { width: 560, height: 760 }, deviceScaleFactor: 2 })).newPage()
  await p.clock.install()
  await p.goto(`${base}/capture/context-trace-push.html?theme=${theme}`)
  await p.clock.runFor(500)
  await expectReads(p, 1)
  return p
}

async function expectReads(p, n) {
  const got = await p.evaluate(() => window.__ctx.reads())
  if (got !== n) throw new Error(`expected ${n} REST read(s), saw ${got}`)
  await p.locator('[data-testid="reads"]', { hasText: `REST reads: ${n}` }).waitFor({ timeout: 5_000 })
}

async function shoot(p, name) {
  await p.clock.runFor(50)
  const file = `${out}/${name}.png`
  await p.locator('[data-capture-root]').screenshot({ path: file })
  console.log(`captured ${file}`)
}

for (const theme of ['dark', 'light']) {
  const p = await open(theme)
  // R1 before: the mount read, nothing else.
  await shoot(p, `r1-mount-${theme}`)

  // R1 after: one usage frame, one read, a new turn on the chart.
  if (await p.evaluate(() => window.__ctx.frame(3)) !== 'requested') throw new Error('frame 3 not requested')
  await p.clock.runFor(COALESCE_MS + 50)
  await expectReads(p, 2)
  await p.locator('text=Turn 3').first().waitFor({ timeout: 5_000 })
  await shoot(p, `r1-frame-${theme}`)

  if (theme === 'light') { await p.context().close(); continue }

  // R2: a duplicate and an older revision are dropped; no read.
  for (const rev of [3, 2]) {
    if (await p.evaluate(r => window.__ctx.frame(r), rev) !== 'stale') throw new Error(`frame ${rev} not stale`)
  }
  await p.clock.runFor(COALESCE_MS + 50)
  await expectReads(p, 2)
  await shoot(p, 'r2-stale-dropped-dark')

  // R3: a burst of five frames across two units is one read.
  for (let r = 4; r <= 8; r++) await p.evaluate(([rev, unit]) => window.__ctx.frame(rev, unit), [r, r % 2 ? 'unit-1' : 'unit-2'])
  await p.clock.runFor(COALESCE_MS + 50)
  await expectReads(p, 3)
  await shoot(p, 'r3-burst-one-read-dark')

  // R4: five minutes pass with no frame; no read.
  await p.clock.runFor(5 * 60_000)
  await p.evaluate(() => window.__ctx.mark('5 min passed on the page clock, no frame'))
  await expectReads(p, 3)
  await shoot(p, 'r4-five-minutes-no-poll-dark')

  // R5: the socket reopens on a restarted gateway; one read, and its revision 1 frame is not stale.
  await p.evaluate(() => window.__ctx.reconnect())
  await p.clock.runFor(50)
  await expectReads(p, 4)
  await shoot(p, 'r5-reconnect-reread-dark')
  if (await p.evaluate(() => window.__ctx.frame(1)) !== 'requested') throw new Error('post-restart rev 1 dropped')
  await p.clock.runFor(COALESCE_MS + 50)
  await expectReads(p, 5)
  await shoot(p, 'r5-restart-rev1-applied-dark')
  await p.context().close()
}
await b.close()
