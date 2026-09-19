/**
 * Screenshot evidence for the Settings > Display credit-meter fallback toggle (#7627).
 *
 * Drives the isolated capture entry (website/capture/credit-meter-toggle.html),
 * which mounts the REAL `DisplayPanel` with the config route answered per scene.
 *
 * Frames:
 *   01-off.png     a config that has never carried the key: the row reads OFF,
 *                  which is the whole point -- installing the control must not
 *                  start billing.
 *   02-on.png      the key stored as a real boolean true: the row reads ON.
 *   03-rejected.png a refused PATCH: the switch has rolled back to OFF and the
 *                  catalog error line is on screen under it.
 *
 * Each frame is ASSERTED, not merely photographed: "reads OFF from an ABSENT
 * key", "rolled back" and "the catalog sentence, not the backend's" are claims
 * about the DOM that a picture cannot settle on its own.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-credit-meter-toggle.mjs http://127.0.0.1:6841 ../temp-screenshots/7627-credit-meter-toggle
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/7627-credit-meter-toggle'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 900, height: 760 }
/** The settings cards stagger in; settle past it so a still is not mid-entrance. */
const STAGGER_SETTLE_MS = 900
const LABEL = 'Spend a few credits to check your balance'
const ERROR_LINE = 'Could not save this setting.'
const OWNER_LINE = 'Only the dashboard owner can turn this on.'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than the
// system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

function fail(msg) {
  console.error(`FAIL: ${msg}`)
  failures += 1
}

async function scene(query) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  page.on('pageerror', e => fail(`page error: ${e.message}`))
  await page.goto(`${BASE}/capture/credit-meter-toggle.html?${query}`, { waitUntil: 'load' })
  const sw = page.getByRole('switch', { name: LABEL })
  await sw.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(STAGGER_SETTLE_MS)
  await sw.scrollIntoViewIfNeeded()
  await page.waitForTimeout(200)
  return { page, sw }
}

/* ── 01 the key is absent: the row reads OFF ───────────────────────────────── */
{
  const { page, sw } = await scene('scene=off&theme=dark')
  if ((await sw.getAttribute('aria-checked')) !== 'false') {
    fail('row does not read OFF when the config has never carried the key')
  }
  const desc = await page.getByText(/Each check spends a small number of credits/).count()
  if (desc !== 1) fail(`cost disclosure not on the row (matched ${desc})`)
  if ((await page.getByText(/about every 10 minutes/).count()) !== 1) {
    fail('cost cadence not stated on the row')
  }
  await page.screenshot({ path: join(OUT, '01-off.png') })
  await page.close()
}

/* ── 02 stored true: the row reads ON ─────────────────────────────────────── */
{
  const { page, sw } = await scene('scene=on&theme=dark')
  if ((await sw.getAttribute('aria-checked')) !== 'true') {
    fail('row does not read ON when the key is stored as a real boolean true')
  }
  await page.screenshot({ path: join(OUT, '02-on.png') })
  await page.close()
}

/* ── 03 refused PATCH: rolled back, with the CATALOG sentence ──────────────── */
{
  const { page, sw } = await scene('scene=reject&theme=dark')
  await sw.click()
  await page.getByText(ERROR_LINE).waitFor({ state: 'visible', timeout: 10000 })
  if ((await sw.getAttribute('aria-checked')) !== 'false') {
    fail('switch did not roll back after the write was refused')
  }
  // The backend's own words must not reach the user: they ship untranslated.
  if ((await page.getByText(/field not editable/).count()) !== 0) {
    fail('backend error text rendered instead of the catalog sentence')
  }
  await sw.scrollIntoViewIfNeeded()
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, '03-rejected.png') })
  await page.close()
}

/* ── 04 refused 403 owner_only: the permission message, not "try again" ────── */
{
  const { page, sw } = await scene('scene=forbidden&theme=dark')
  await sw.click()
  await page.getByText(OWNER_LINE).waitFor({ state: 'visible', timeout: 10000 })
  if ((await page.getByText(/you can try again/).count()) !== 0) {
    fail('generic retry line shown for a refusal a retry cannot fix')
  }
  if ((await sw.getAttribute('aria-checked')) !== 'false') {
    fail('switch did not roll back after the refusal')
  }
  await sw.scrollIntoViewIfNeeded()
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, '04-owner-only.png') })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed -- frames are not admissible evidence.`)
  process.exit(1)
}
console.log(`wrote 4 asserted frames to ${OUT}`)
