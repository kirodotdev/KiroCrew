// Typing-bounce repro rig: pinned at bottom on a phone viewport, type a long
// multi-line message into the composer, record per-keystroke: scrollTop of
// the chat scroller, composer height, count of visible skeleton/fallback
// surfaces, and mounted-row churn. The verdict discriminates:
//   A) composer line-wrap -> viewport shrink -> window recompute (geometry)
//   B) per-keystroke re-render -> row remount -> staging skeleton (identity)
import fs from 'node:fs'
import { bootPhoneRig } from './rig-lib.mjs'

const url = fs.readFileSync('/tmp/kc-pod-url.txt', 'utf8').trim()
const { context, page } = await bootPhoneRig(url)

// Land at bottom, let boot noise settle.
await page.waitForTimeout(4000)

const probe = async () =>
  page.evaluate(() => {
    const scroller = [...document.querySelectorAll('*')].find(
      (e) => e.scrollHeight > e.clientHeight * 2 && /auto|scroll/.test(getComputedStyle(e).overflowY)
        && e.querySelector('[data-index]'),
    )
    const composer = document.querySelector('textarea')
    const skeletons = document.querySelectorAll(
      '[class*="skeleton"], [class*="animate-pulse"], [aria-busy="true"]',
    ).length
    const invisibleWarm = document.querySelectorAll('.invisible.absolute').length
    const rows = [...document.querySelectorAll('[data-index]')].map((r) => r.getAttribute('data-index'))
    return {
      st: scroller ? Math.round(scroller.scrollTop) : -1,
      sh: scroller ? scroller.scrollHeight : -1,
      ch: scroller ? scroller.clientHeight : -1,
      composerH: composer ? composer.getBoundingClientRect().height : -1,
      skeletons,
      invisibleWarm,
      rowLo: rows.length ? rows[0] : null,
      rowHi: rows.length ? rows[rows.length - 1] : null,
      rowN: rows.length,
    }
  })

const base = await probe()
console.error('BASE', JSON.stringify(base))

// Focus the composer.
await page.click('textarea')
await page.waitForTimeout(300)

// Type a message long enough to wrap several times at 390px. Log per burst.
const text = 'The quick brown fox jumps over the lazy dog while the archive keeps loading pages and the reader keeps their place without any bounce at all, hopefully, across several wrapped lines of composer growth. '
let prev = await probe()
let events = 0
for (let i = 0; i < text.length; i += 8) {
  await page.keyboard.type(text.slice(i, i + 8), { delay: 15 })
  const cur = await probe()
  const dSt = cur.st - prev.st
  const dCh = cur.ch - prev.ch
  const dComposer = Math.round(cur.composerH - prev.composerH)
  const dSkel = cur.skeletons - prev.skeletons
  const windowMoved = cur.rowLo !== prev.rowLo || cur.rowHi !== prev.rowHi
  if (dSt !== 0 || dCh !== 0 || dComposer !== 0 || dSkel !== 0 || windowMoved) {
    events++
    console.error(
      `EVT i=${i} dScrollTop=${dSt} dClientH=${dCh} dComposerH=${dComposer} dSkel=${dSkel} window=${prev.rowLo}..${prev.rowHi} -> ${cur.rowLo}..${cur.rowHi} warm=${cur.invisibleWarm}`,
    )
  }
  prev = cur
}
console.error('DONE events=', events, 'FINAL', JSON.stringify(await probe()))
await context.close()
