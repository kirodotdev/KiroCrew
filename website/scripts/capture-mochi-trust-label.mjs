/**
 * Screenshots of Mochi's approval-card exact-trust label (#4462).
 *
 * Drives the isolated capture entry (website/capture/mochi-trust-label.html),
 * which mounts the REAL ChatPanel Bubble with a real `__approval__` payload —
 * the same parse and the same `truncateCommandLabel` the shipped card uses.
 *
 * Each scene opens the Trust scope rows, ASSERTS ITS RENDERED LABEL before
 * writing the file, and the run additionally asserts that the two scenes — two
 * DIFFERENT commands sharing a long prefix — do not render the SAME label.
 * That cross-scene check is the property the change exists to restore, and it
 * fails on the old 30-char budget, so a before-frame cannot be mistaken for a
 * passing one (pass --no-expect-distinct to shoot before-frames).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-mochi-trust-label.mjs http://127.0.0.1:6821 ../temp-screenshots/mochi-trust-label
 *
 * The `quoted` scene asserts that a command ending in '"' renders
 * the closing quote ONCE, not doubled by the former catalog delimiter.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/mochi-trust-label'
const PREFIX = process.argv[4] || ''
const EXPECT_DISTINCT = !process.argv.includes('--no-expect-distinct')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'mochi-approval-api-config', cmd: 'api_config', pair: true },
  { name: 'mochi-approval-api-secrets', cmd: 'api_secrets', pair: true },
  // #4700: a run of whitespace inside the granted string. `runOf` asserts the
  // RENDERED text still carries it -- the DOM text is exact either way, so a
  // collapse is visible only through a real browser's innerText.
  { name: 'mochi-approval-spaced', cmd: 'spaced', runOf: '  ' },
  // #12371: a command ending in a straight double-quote.  `endsQuote: true`
  // asserts the label does NOT end with a double-quote (which would indicate
  // the catalog's former closing delimiter is still there).
  { name: 'mochi-approval-quoted', cmd: 'quoted', endsQuote: true },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 460, height: 340 }, deviceScaleFactor: 2 })

let failed = false
const labels = []
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/mochi-trust-label.html?cmd=${s.cmd}`)
  await page.waitForSelector('[data-capture-root]')
  // Reveal the scoped trust rows: the exact-command label only exists once open.
  await page.getByRole('button', { name: /Trust/ }).first().click()
  // The exact-command row is the button whose label carries the quoted command
  // (apps.mochi.approval.trust_this_command). Located by TEXT, not by [title]:
  // the before-frames run against the base code, which has no title attribute.
  const exactBtn = page.getByRole('button', { name: /^Trust / }).first()
  await exactBtn.waitFor()
  const label = (await exactBtn.innerText()).trim()
  const title = await exactBtn.getAttribute('title')
  // The label must be the real trust row in BOTH modes — a frame whose label is
  // wrong is misleading evidence regardless of before/after. Only the tooltip is
  // part of the fix, so only IT is gated on after-mode.
  if (!label.startsWith('Trust')) {
    console.log(`${s.name}: label=${JSON.stringify(label)} MISMATCH`)
    failed = true
    continue
  }
  // Whitespace is part of an exact-string grant, so a collapsed run is a
  // misleading frame: gate on it in after-mode, the same way the tooltip is.
  const keptRun = !s.runOf || label.includes(s.runOf)
  // #12371: a command ending in '"' should show that quote ONCE — not doubled
  // by a catalog delimiter that is now gone.  The correct label ends with
  // the command's last character ('"') and has nothing appended after it.
  // `!label.endsWith('"' + '"')` catches ASCII doubling; also check for the
  // RIGHT DOUBLE QUOTATION MARK (U+201D) that English and pt catalogs used.
  const noDoubledQuote = !s.endsQuote || (
    !label.endsWith('"\u201d') &&  // ASCII " then U+201D (was: English/pt catalog close)
    !label.endsWith('"\u00bb') &&  // ASCII " then U+00BB (was: es/it/ru/bn/hi)
    !label.endsWith('"\u300d') &&  // ASCII " then U+300D (was: ja)
    !label.endsWith('"\u2019')     // ASCII " then U+2019 (was: ko)
  )
  const ok = (!EXPECT_DISTINCT || Boolean(title && title.includes(s.runOf || s.endsQuote ? 'grep' : 'gh api')))
    && (!EXPECT_DISTINCT || keptRun)
    && noDoubledQuote
  console.log(
    `${s.name}: label=${JSON.stringify(label)} title=${JSON.stringify(title)} ` +
    `keptRun=${s.runOf ? keptRun : '-'} noDoubledQuote=${s.endsQuote ? noDoubledQuote : '-'} ${ok ? 'OK' : 'MISMATCH'}`,
  )
  if (!ok) { failed = true; continue }
  if (s.pair) labels.push(label)
  await page.screenshot({ path: `${OUT}/${PREFIX}${s.name}.png` })
}

if (labels.length === 2) {
  const distinct = labels[0] !== labels[1]
  console.log(`api pair distinguishable: ${distinct}`)
  if (EXPECT_DISTINCT && !distinct) {
    console.error('the two api commands render the SAME label — the reader cannot tell them apart')
    failed = true
  }
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected label — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
