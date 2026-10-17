// Capture evidence for the top-bar metrics popover, and assert the popover is
// actually rendered where a user could read it. Run against an isolated dev
// stack (dev-fullstack.sh), never the live gateway.
//   ./dev-fullstack.sh                                   # in another shell
//   node scripts/capture-metrics-popover.mjs "http://localhost:3000/?token=..." \
//     ../temp-screenshots/metrics-popover
//
// Assertions, per group width:
//  - wide: the probe is visible, the control is the inline toggle
//    (aria-pressed, no aria-haspopup), and a click expands the readings
//  - narrow: the rung has dropped the readings and the control has become a
//    popover trigger (aria-haspopup=dialog)
//  - the popover is on screen, anchored under and right-aligned to the trigger,
//    opaque, sized, and carries CPU / MEM / DSK plus the absolute GB figures
//  - Escape dismisses it
//
// The kiro-prerequisite probe is stubbed ready: this host cannot build a nested
// sandbox, so the real gate replaces the whole shell and there is no top bar to
// photograph. The stub is confined to that one gate -- every reading in the
// frames comes from the real /api/system.
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const url = process.argv[2]
const outDir = process.argv[3] || '../temp-screenshots/metrics-popover'
mkdirSync(outDir, { recursive: true })

const READY_PREREQ = {
  platform: 'Linux',
  installed: true,
  authenticated: true,
  ready: true,
  initial_setup_complete: true,
  repair_required: false,
  docs_url: 'https://kiro.dev/cli/',
  login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  setup_allowed: true,
  sandbox_unavailable: false,
  sandbox_failure_kind: '',
  sandbox_detail: '',
  sandbox_remedy: '',
  missing_agent_specs: [],
  agent_spec_repair_error: '',
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1 })
await ctx.route('**/api/kiro-prerequisite*', route =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(READY_PREREQ) }))
// Same seed playwright/auth.setup.ts uses, so the first-run chapters do not
// overlay the shell.
await ctx.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})
const page = await ctx.newPage()

await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.tb-right', { timeout: 30000 })
await page.waitForTimeout(3000)

// A first-run chapter can still be re-derived from server state after theme
// boot. Walk it forward (Escape is refused on the mandatory ones).
for (let i = 0; i < 10; i++) {
  const dlg = page.locator('[role="dialog"][aria-modal="true"]').first()
  if (!(await dlg.count())) break
  const btn = dlg.getByRole('button', { name: /Done|Skip all|Finish|Continue|Got it|Next/ }).first()
  if (await btn.count()) await btn.click({ timeout: 5000 }).catch(() => {})
  else await page.keyboard.press('Escape')
  await page.waitForTimeout(700)
}
// The session-expired banner is a full-width interceptor; dismiss it if present.
await page.evaluate(() => document.getElementById('mc-session-expired')?.remove())

const shot = async (name, clip) => {
  const path = `${outDir}/${name}.png`
  await page.screenshot({ path, clip })
  console.log('WROTE', path)
}

const topRight = async () => {
  const box = await page.locator('.tb-right').boundingBox()
  if (!box) throw new Error('.tb-right not found')
  const x = Math.max(0, box.x - 30)
  return { x, y: 0, width: Math.min(700, page.viewportSize().width - x), height: 190 }
}

const state = () => page.evaluate(() => {
  const g = document.querySelector('.tb-right')
  const cs = getComputedStyle(g)
  const probe = document.querySelector('.tb-metrics-probe')
  const readings = document.querySelector('.tb-drop-metrics:not(.tb-metrics-probe)')
  const btn = document.querySelector('[aria-label="System metrics"]')
  return {
    groupContentWidth: Math.round(g.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
    probeDisplay: probe ? getComputedStyle(probe).display : 'NO PROBE',
    readingsVisible: !!readings && getComputedStyle(readings).display !== 'none',
    readingsText: readings ? readings.textContent.replace(/\s+/g, ' ').trim() : '',
    ariaPressed: btn?.getAttribute('aria-pressed') ?? null,
    ariaHasPopup: btn?.getAttribute('aria-haspopup') ?? null,
  }
})

const clickMetrics = async () => {
  await page.locator('[aria-label="System metrics"]').first().click()
  await page.waitForTimeout(700)
}

const assert = (cond, msg) => { if (!cond) throw new Error(`ASSERT FAILED: ${msg}`) }

// 1. Wide group: the readings fit, so the control is still the inline toggle.
let s = await state()
console.log('WIDE(off)  ', JSON.stringify(s))
assert(s.probeDisplay !== 'none', 'wide: probe should be visible')
assert(s.ariaHasPopup === null && s.ariaPressed === 'false', 'wide: control must be the toggle, not a popover trigger')
await clickMetrics()
s = await state()
console.log('WIDE(on)   ', JSON.stringify(s))
assert(s.readingsVisible, 'wide: click must expand the inline readings')
await shot('01-wide-inline-readings', await topRight())

// 2. Narrow group: the ladder drops the readings. This is the band where the
//    old toggle recoloured the icon and expanded nothing.
await page.setViewportSize({ width: 1100, height: 900 })
await page.waitForTimeout(1500)
s = await state()
console.log('NARROW     ', JSON.stringify(s))
assert(s.probeDisplay === 'none', 'narrow: the rung must have dropped the readings')
assert(s.ariaHasPopup === 'dialog', 'narrow: control must become a popover trigger')
await shot('02-narrow-collapsed', await topRight())

// 3. Same width, popover open -- and readable.
await clickMetrics()
await page.waitForSelector('[role="dialog"][aria-label="System metrics"]', { timeout: 5000 })
const pop = await page.evaluate(() => {
  const d = document.querySelector('[role="dialog"][aria-label="System metrics"]')
  const r = d.getBoundingClientRect()
  const b = document.querySelector('[aria-label="System metrics"]').getBoundingClientRect()
  const cs = getComputedStyle(d)
  return {
    text: d.textContent.replace(/\s+/g, ' ').trim(),
    rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    inViewport: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
    belowTrigger: r.top >= b.bottom,
    rightAligned: Math.abs(r.right - b.right) < 40,
    opaque: cs.backgroundColor !== 'rgba(0, 0, 0, 0)',
  }
})
console.log('POPOVER    ', JSON.stringify(pop))
assert(pop.inViewport, 'popover must be fully on screen')
assert(pop.belowTrigger && pop.rightAligned, 'popover must be anchored under the trigger')
assert(pop.opaque, 'popover must be opaque, not a transparent overlay')
assert(pop.rect.w > 120 && pop.rect.h > 50, 'popover must have real size')
for (const label of ['CPU', 'MEM', 'DSK', 'GB']) {
  assert(pop.text.includes(label), `popover must show ${label}`)
}
await shot('03-narrow-popover-open', await topRight())

// 4. Escape dismisses it.
await page.keyboard.press('Escape')
await page.waitForTimeout(400)
assert(!(await page.locator('[role="dialog"][aria-label="System metrics"]').count()), 'Escape must dismiss the popover')
console.log('escape dismissed: ok')

await browser.close()
console.log('done')
