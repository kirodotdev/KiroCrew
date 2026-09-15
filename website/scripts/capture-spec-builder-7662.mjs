/**
 * Screenshots for the Spec Builder #7662 fixes.
 *
 * Drives the isolated capture entry (website/capture/spec-builder-7662.html),
 * which mounts the REAL SpecDetail / SpecRail. Every frame asserts its state
 * before writing, so a frame cannot document the wrong state:
 *   01-delete-error-dark   the refused delete rendered INSIDE the confirm
 *                          dialog (translated lead + reason), page-top setErr
 *                          banner NOT invoked
 *   02-delete-error-light  light theme parity
 *   03-delete-error-390    the same dialog at a 390px viewport
 *   04-rail-empty-filter   "no specs match" empty state with the Clear-filter
 *                          action
 *   05-rail-cleared        clicking Clear filter emptied the input and
 *                          restored the full list
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort   # in another shell
 *   node scripts/capture-spec-builder-7662.mjs http://127.0.0.1:6832 ../temp-screenshots/spec-builder-7662
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/spec-builder-7662'
mkdirSync(OUT, { recursive: true })

const DETAIL = {
  name: 'checkout-flow',
  title: 'Checkout flow',
  phase: 'design',
  status: 'planning',
  running: false,
  working_dir: '/home/dev/checkout-flow',
  spec_dir: '/home/dev/checkout-flow/.kiro/specs/checkout-flow',
  slot_key: 'spec-builder-checkout-flow-1',
  duplicate_supported: true,
  files: { 'requirements.md': '# Requirements', 'design.md': '# Design' },
  docs: {
    'requirements.md': { hash: 'a'.repeat(64) },
    'design.md': { hash: 'b'.repeat(64) },
  },
}

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(scene, theme, viewport = { width: 1280, height: 820 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 })
  // Gateway-free: answer every REAL API call the mounted components make.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (req.method() === 'DELETE' && path.includes('/specs/checkout-flow')) {
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'the worker slot is mid-turn — pause the build first' }),
      })
    }
    if (path === '/api/apps/spec-builder/specs/checkout-flow') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DETAIL) })
    }
    const isList = /commands|skills|agents|sessions|files|history|models|specs$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/spec-builder-7662.html?scene=${scene}&theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  return page
}

/** Open the delete confirm, click Remove, and wait for the in-dialog alert. */
async function refusedDelete(page) {
  // Text, not role: at narrow widths the title is a static span, not the
  // rename button.
  await page.getByText('Checkout flow', { exact: true }).first().waitFor()
  await page.getByRole('button', { name: /more actions/i }).click()
  await page.getByRole('menuitem', { name: 'Remove spec checkout-flow' }).click()
  const dialog = page.getByRole('dialog', { name: 'Remove this spec?' })
  await dialog.waitFor()
  await page.getByRole('button', { name: 'Remove this spec' }).click()
  await dialog.getByRole('alert').waitFor()
  return dialog
}

// 01 — refused delete renders inside the dialog (dark)
{
  const page = await newPage('detail', 'dark')
  const dialog = await refusedDelete(page)
  const alert = await dialog.getByRole('alert').textContent()
  check('01 translated lead', /Couldn’t remove this spec — try again\./.test(alert || ''), 'lead present')
  check('01 reason detail', /worker slot is mid-turn/.test(alert || ''), 'reason present')
  const banner = await page.evaluate(() => window.__setErrCalls)
  check('01 no page-top banner', banner.length === 0, `setErr calls=${banner.length}`)
  await page.screenshot({ path: `${OUT}/01-delete-error-dark.png` })
  await page.close()
}

// 02 — light theme parity
{
  const page = await newPage('detail', 'light')
  await refusedDelete(page)
  await page.screenshot({ path: `${OUT}/02-delete-error-light.png` })
  await page.close()
}

// 03 — the dialog at a 390px viewport
{
  const page = await newPage('detail', 'dark', { width: 390, height: 780 })
  const dialog = await refusedDelete(page)
  const box = await dialog.boundingBox()
  check('03 dialog fits 390', !!box && box.width <= 390, `dialog width=${box?.width}`)
  await page.screenshot({ path: `${OUT}/03-delete-error-390.png` })
  await page.close()
}

// 04/05 — the rail's empty-filter state offers, and executes, Clear filter
{
  const page = await newPage('rail', 'dark')
  await page.getByText('Checkout flow').waitFor()
  await page.getByRole('textbox', { name: /filter specs by name/i }).fill('zebra')
  await page.getByTestId('filtered-empty').waitFor()
  const clear = page.getByRole('button', { name: /clear filter/i })
  check('04 clear-filter offered', await clear.isVisible(), 'button present in empty state')
  await page.screenshot({ path: `${OUT}/04-rail-empty-filter.png` })

  await clear.click()
  await page.getByText('Checkout flow').waitFor()
  const value = await page.getByRole('textbox', { name: /filter specs by name/i }).inputValue()
  check('05 input emptied', value === '', `value=${JSON.stringify(value)}`)
  const emptyGone = (await page.getByTestId('filtered-empty').count()) === 0
  check('05 list restored', emptyGone, 'empty state gone, groups back')
  await page.screenshot({ path: `${OUT}/05-rail-cleared.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
