/**
 * Golden frames for ChatPage's transcript scroll shell.
 *
 * CAPTURE (default): shoots the shell's visual states off the isolated entry
 * (capture/chatpage-scroll-shell.html — the REAL ChatPage, deterministic
 * fixture, animations off). Every frame asserts its state before writing:
 *   01-bottom       long transcript auto-pinned to the bottom (±2px), bottom
 *                   mask present above the composer
 *   02-pill         scrolled up 600px: jump pill visible, top fade under header
 *   03-short        transcript too short to scroll: fades still lay out sanely
 *   (x2 themes: dark, light)
 *
 * COMPARE (--compare <baselineDir> <candidateDir>): byte-compares same-named
 * frames — the migration gate. Run capture on the base commit into A, on the
 * migrated commit into B (same machine, same Chromium), then compare. Any
 * mismatch exits 1 and names the frame; the two files are on disk for a human
 * or reviewer to eyeball.
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort     # another shell
 *   node scripts/capture-chatpage-scroll-shell.mjs http://127.0.0.1:6832 ../temp-screenshots/chatpage-scroll-shell-golden
 *   node scripts/capture-chatpage-scroll-shell.mjs --compare dirA dirB
 */
import { chromium } from 'playwright'
import { resolve as resolvePath } from 'node:path'
import { mkdirSync, readFileSync, readdirSync, rmSync, renameSync, existsSync } from 'node:fs'

const EXPECTED_FRAMES = ['dark', 'light'].flatMap(t => [`01-bottom-${t}.png`, `02-pill-${t}.png`, `03-short-${t}.png`, `04-paging-${t}.png`]).sort()

if (process.argv[2] === '--compare') {
  const [a, b] = [process.argv[3], process.argv[4]]
  if (!a || !b) { console.error('--compare needs <baselineDir> <candidateDir>'); process.exit(1) }
  if (resolvePath(a) === resolvePath(b)) { console.error('--compare needs two DIFFERENT directories (same dir passes trivially)'); process.exit(1) }
  // A vacuous pass is worse than a failure: an empty baseline, a missing
  // candidate frame, or a stray extra frame must all fail loudly. Both dirs
  // must contain EXACTLY the expected frames, each non-empty.
  let bad = 0
  for (const [name, dir] of [['baseline', a], ['candidate', b]]) {
    let got
    try { got = readdirSync(dir).filter(f => f.endsWith('.png')).sort() }
    catch { console.error(`${name} dir ${dir} is not readable — check the path`); process.exit(1) }
    if (JSON.stringify(got) !== JSON.stringify(EXPECTED_FRAMES)) {
      console.error(`${name} dir ${dir} does not hold exactly the expected frames.\n  expected: ${EXPECTED_FRAMES.join(', ')}\n  got:      ${got.join(', ') || '(none)'}`)
      bad++
    }
  }
  if (bad) process.exit(1)
  for (const f of EXPECTED_FRAMES) {
    const [fa, fb] = [readFileSync(`${a}/${f}`), readFileSync(`${b}/${f}`)]
    if (!fa.length || !fb.length) { console.error(`${f}: empty file`); bad++; continue }
    const same = fa.equals(fb)
    console.log(`${f}: ${same ? 'identical' : 'DIFFERS'}`)
    if (!same) bad++
  }
  process.exit(bad ? 1 : 0)
}

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const FINAL_OUT = process.argv[3] || '../temp-screenshots/chatpage-scroll-shell-golden'
// Shoot into a temp dir and PROMOTE only after every check passes: writing
// frames straight to the target means a failed run still overwrites the
// goldens, and a later --compare then blesses wrong-state pixels.
const OUT = `${FINAL_OUT}.capturing-${process.pid}`
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false
const check = (name, ok, detail) => { console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`); if (!ok) failed = true }

const SCROLLER = '.chat-container'

/** Must be byte-equal to the entry's fixture: ChatPage refetches the slot on
 *  mount and the response REPLACES chat.messages, so a null/empty answer here
 *  silently blanks the preloaded transcript (the continueGate suite's trap). */
const pad = (n) => String(n).padStart(2, '0')
const fixture = (scene) => {
  const long = Array.from({ length: 16 }, (_, i) => ([
    { role: 'user', content: `Status check #${i + 1}: anything new in the queue?`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:00Z` },
    { role: 'assistant', content: `Sweep ${i + 1} done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:30Z` },
  ])).flat()
  return scene === 'short' ? long.slice(0, 2) : long
}
const geom = (page) => page.evaluate((sel) => {
  const el = document.querySelector(sel)
  return el ? { top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight } : null
}, SCROLLER)

async function newPage(theme, scene) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      const msgs = fixture(scene)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: 'slot-a', title: 'scroll-shell fixture', running: false, has_more: scene === 'paging', total: msgs.length, messages: msgs }) })
    }
    // Bare-array endpoints: their consumers type the response as T[] directly.
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/chatpage-scroll-shell.html?theme=${theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(scene === 'short' ? 'Status check #1:' : 'Sweep 16 done.').first().waitFor()
  await page.waitForTimeout(250) // one RO/layout settle; animations are globally off
  // ThemeProvider re-derives the root theme from its own preference store —
  // verify the REQUESTED mode actually survived provider mount, or the
  // "dark" frames are silently light duplicates. The resolved attribute is
  // "<colorTheme>-<mode>" (e.g. kiro-dark), so match the mode suffix.
  const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  check(`theme-applied ${theme}/${scene}`, applied === theme || !!applied?.endsWith(`-${theme}`), `data-theme=${applied}`)
  // The entry's fixture and this script's route answer MUST be byte-equal
  // (the mount refetch REPLACES the store messages) — enforce instead of
  // trusting the duplicated literals to stay in sync.
  const parity = await page.evaluate(() => JSON.stringify(window.__CAPTURE_MESSAGES__))
  check(`fixture-parity ${theme}/${scene}`, parity === JSON.stringify(fixture(scene)), parity === JSON.stringify(fixture(scene)) ? 'ok' : 'entry fixture != script fixture')
  return page
}

for (const theme of ['dark', 'light']) {
  // 01 — long transcript lands at the bottom
  {
    const page = await newPage(theme, 'long')
    const g = await geom(page)
    check(`01-${theme} scroller`, !!g && g.height > g.client, JSON.stringify(g))
    check(`01-${theme} pinned to bottom`, !!g && Math.abs(g.top - (g.height - g.client)) <= 2, `top=${g?.top} bottom=${g ? g.height - g.client : '-'}`)
    const mask = await page.locator('[data-capture-root] .bg-gradient-to-t.from-bg').count()
    check(`01-${theme} bottom mask mounted`, mask >= 1, `masks=${mask}`)
    await page.screenshot({ path: `${OUT}/01-bottom-${theme}.png` })
    await page.close()
  }
  // 02 — scrolled up: pill + top fade
  {
    const page = await newPage(theme, 'long')
    await page.evaluate((sel) => {
      // ABSOLUTE offset, not bottom-relative: the pinned-prompt band updates in
      // a scroll rAF, and a bottom-relative target lands the boundary bubble a
      // sub-pixel apart between runs — the one nondeterminism the golden
      // comparison cannot tolerate. 1200 puts the boundary mid-bubble
      // deterministically.
      const el = document.querySelector(sel)
      el.scrollTop = 1200
      el.dispatchEvent(new Event('scroll'))
    }, SCROLLER)
    await page.getByLabel('Scroll to bottom').waitFor()
    // Let the pinned-band rAF chain settle before shooting.
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
    await page.waitForTimeout(120)
    const topFade = await page.locator('[data-capture-root] .bg-gradient-to-b.from-bg').count()
    check(`02-${theme} top fade mounted`, topFade >= 1, `fades=${topFade}`)
    await page.screenshot({ path: `${OUT}/02-pill-${theme}.png` })
    await page.close()
  }
  // 03 — short transcript: nothing to scroll, no pill, fades behave
  {
    const page = await newPage(theme, 'short')
    const g = await geom(page)
    check(`03-${theme} no scroll needed`, !!g && g.height <= g.client + 2, JSON.stringify(g))
    const pill = await page.getByLabel('Scroll to bottom').count()
    check(`03-${theme} no pill`, pill === 0, `pills=${pill}`)
    await page.screenshot({ path: `${OUT}/03-short-${theme}.png` })
    await page.close()
  }
  // 04 — paging state: the two pieces the extraction moved across the file
  // boundary are BOTH on screen — the earlier-messages bar (aboveRows slot)
  // and the sticky older-messages spinner (loadingOlder).
  {
    const page = await newPage(theme, 'paging')
    await page.evaluate((sel) => { const el = document.querySelector(sel); el.scrollTop = 0; el.dispatchEvent(new Event('scroll')) }, SCROLLER)
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
    const bar = await page.getByTestId('load-earlier-messages').count()
    check(`04-${theme} earlier-messages bar mounted`, bar === 1, `bars=${bar}`)
    const spinner = await page.getByTestId('older-messages-loading').count()
    check(`04-${theme} older-messages spinner mounted`, spinner === 1, `spinners=${spinner}`)
    const spinnerVisible = spinner === 1 && await page.getByTestId('older-messages-loading').isVisible()
    check(`04-${theme} spinner visible (sticky under header)`, spinnerVisible, '')
    await page.screenshot({ path: `${OUT}/04-paging-${theme}.png` })
    await page.close()
  }
}

await browser.close()
// The two themes must produce DIFFERENT pixels: identical pairs mean the
// theme never took effect and the "dark" goldens are counterfeit light ones.
// Derived from EXPECTED_FRAMES so a new scene cannot ship without this check.
for (const frame of [...new Set(EXPECTED_FRAMES.map(f => f.replace(/-(dark|light)\.png$/, '')))]) {
  const same = readFileSync(`${OUT}/${frame}-dark.png`).equals(readFileSync(`${OUT}/${frame}-light.png`))
  check(`${frame} dark differs from light`, !same, same ? 'byte-identical' : 'ok')
}
if (failed) {
  console.error(`CAPTURE FAILED: a frame did not match its asserted state — goldens NOT updated (frames left in ${OUT})`)
  process.exit(1)
}
// Promote by directory swap, not per-file copy: an interrupted copy would
// leave a MIXED old/new baseline, and a renamed frame would leave a ghost
// PNG. Keep the previous baseline as .old until the swap lands, then drop it.
const OLD = `${FINAL_OUT}.old-${process.pid}`
if (existsSync(FINAL_OUT)) renameSync(FINAL_OUT, OLD)
renameSync(OUT, FINAL_OUT)
rmSync(OLD, { recursive: true, force: true })
console.log(`all golden frames verified — promoted to ${FINAL_OUT}`)
