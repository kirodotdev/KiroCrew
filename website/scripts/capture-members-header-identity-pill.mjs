/**
 * Screenshot harness for the Crew Members DM header's centred identity pill:
 * the crewmate's face and name sit in ONE Liquid Glass chip in the middle of
 * the header, the chip itself is the "Edit crewmate" button (no separate
 * pencil), and the back button and the panel opener stay bare at the edges.
 * Against a REAL pod, not fixtures.
 *
 * What the evidence has to show, per frame:
 *   - the pill is a Glass pane AND a button named by the crewmate (its
 *     content) and described "Edit crewmate" (its tooltip), holding the face
 *     and the name, with no pencil anywhere in the header;
 *   - it is centred on the header whether the panel opener is present (panel
 *     hidden) or absent (panel docked open), and on a narrow window where the
 *     back button sits at the left edge;
 *   - hovering the pill changes nothing but the cursor (the Glass material has
 *     no hover state), and clicking it opens this crewmate's full editor;
 *   - with Reduce glass transparency on, the pill solidifies with the rest of
 *     the glass instead of staying translucent.
 *
 * Usage:
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-members-header-identity-pill.mjs ../temp-screenshots/members-header-identity-pill
 *
 * Every frame asserts what it photographs before writing the PNG.
 */
import { chromium, devices } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, podInfo, primeCrewPod } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-header-identity-pill'
const CREW = 'oncall'
mkdirSync(OUT, { recursive: true })

const { BASE, authed } = podInfo(readFileSync)

async function openMember(page) {
  // A fresh pod home fires the Meet CrewMates first-run chapter over the page
  // once the pod has a crewmate; mark it seen server-side and dismiss by name
  // if it is already up, never by a blind Escape.
  await page.evaluate(async () => {
    localStorage.setItem('mc-crewmates-onboarded', '1')
    await fetch('/api/config/theme', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ crewmates_onboarded: true }) })
  })
  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  const notNow = page.getByTestId('meet-crewmates-not-now')
  if (await notNow.waitFor({ state: 'visible', timeout: 2500 }).then(() => true, () => false)) {
    await notNow.click()
    await notNow.waitFor({ state: 'hidden', timeout: 10000 })
  }
  const row = page.locator('#main-content li button', { hasText: CREW }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const pill = page.getByTestId('member-identity-pill')
  await pill.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600) // the glass layers size themselves after mount
  // A first-run gate sitting over the page would still pass every DOM check
  // above while hiding the header in the frame — refuse to shoot through one.
  check('no dialog is open over the page', (await page.getByRole('dialog').count()) === 0)
  return pill
}

/** |pill centre − header centre| in CSS px. */
async function offCentre(page) {
  const h = await page.getByTestId('member-thread-header').boundingBox()
  const p = await page.getByTestId('member-identity-pill').boundingBox()
  return Math.abs((p.x + p.width / 2) - (h.x + h.width / 2))
}

async function shootHeader(page, path) {
  const box = await page.getByTestId('member-thread-header').boundingBox()
  await page.screenshot({ path, clip: { x: box.x, y: box.y, width: box.width, height: box.height + 72 } })
}

async function stills(browser, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await primeCrewPod(page, authed, CREW, theme)
  const pill = await openMember(page)

  check(`[${theme}] the pill is a Glass pane`, await pill.evaluate(el => el.classList.contains('liquid-glass')))
  check(`[${theme}] the pill is a button named by the crewmate, described "Edit crewmate"`,
    (await pill.evaluate(el => el.tagName)) === 'BUTTON' && (await pill.getAttribute('aria-label')) === null
      && (await pill.innerText()).includes(CREW) && (await pill.getAttribute('title')) === 'Edit crewmate')
  check(`[${theme}] the pill holds the face and the name`,
    await pill.evaluate(el => !!el.querySelector('img') && !!el.querySelector('[data-testid="member-title-row"]')))
  check(`[${theme}] no pencil anywhere in the header`, (await page.getByTestId('member-edit-name-button').count()) === 0)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)

  // 1: wide, panel docked open → no opener; the pill is centred.
  const withPanel = await offCentre(page)
  check(`[${theme}] centred with the panel open (off by ${withPanel.toFixed(1)}px)`, withPanel <= 1.5)
  await page.screenshot({ path: join(OUT, `01-dm-panel-open-${theme}.png`) })
  await shootHeader(page, join(OUT, `01b-header-panel-open-${theme}.png`))

  // 2: hide the panel → the opener appears at the right; the pill does not move.
  await page.getByTestId('side-panel-root').getByRole('button', { name: 'Close panel' }).click()
  await page.getByTestId('member-panel-toggle').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500)
  const withOpener = await offCentre(page)
  check(`[${theme}] centred with the opener present (off by ${withOpener.toFixed(1)}px)`, withOpener <= 1.5)
  await page.screenshot({ path: join(OUT, `02-dm-panel-hidden-${theme}.png`) })
  await shootHeader(page, join(OUT, `02b-header-panel-hidden-${theme}.png`))

  // 3: hover the pill → same glass (no tint step); the cursor is the affordance.
  const restTint = await pill.evaluate(el => getComputedStyle(el).getPropertyValue('--glass-tint'))
  await pill.hover()
  await page.waitForTimeout(350)
  const hoverTint = await pill.evaluate(el => getComputedStyle(el).getPropertyValue('--glass-tint'))
  check(`[${theme}] the pill keeps its tint under the pointer`, hoverTint === restTint, `${restTint} -> ${hoverTint}`)
  check(`[${theme}] pointer cursor on the pill`, (await pill.evaluate(el => getComputedStyle(el).cursor)) === 'pointer')
  await shootHeader(page, join(OUT, `03-header-hover-${theme}.png`))
  // 3b: click → the crew manager opens THIS crewmate's full editor.
  await pill.click()
  const editor = page.getByRole('dialog', { name: `Edit agent ${CREW}` })
  await editor.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] click opens the crew editor for ${CREW}`, true)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, `03b-click-opens-editor-${theme}.png`) })
  await page.goBack({ waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500)

  // 4: Reduce glass transparency → the pill is a solid card, no glass layers.
  await page.evaluate(() => document.documentElement.setAttribute('data-reduce-transparency', 'on'))
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  const layerShown = await pill.evaluate(el => [...el.querySelectorAll('[data-liquid-glass-layer]')].some(l => getComputedStyle(l).display !== 'none'))
  check(`[${theme}] solid with reduced transparency (no glass layer visible)`, !layerShown)
  await shootHeader(page, join(OUT, `04-header-reduced-transparency-${theme}.png`))
  await context.close()
}

/** Narrow: the back button sits at the left edge; the pill is still centred. */
async function narrow(browser, theme) {
  const context = await browser.newContext({ ...devices['Pixel 7'], deviceScaleFactor: 2 })
  const page = await context.newPage()
  await primeCrewPod(page, authed, CREW, theme)
  await openMember(page)
  check(`[${theme}/narrow] back button present`, await page.getByTestId('member-back').isVisible())
  const off = await offCentre(page)
  check(`[${theme}/narrow] centred beside the back button (off by ${off.toFixed(1)}px)`, off <= 1.5)
  await page.screenshot({ path: join(OUT, `05-narrow-${theme}.png`) })
  await shootHeader(page, join(OUT, `05b-narrow-header-${theme}.png`))
  await context.close()
}

async function main() {
  const browser = await chromium.launch()
  try {
    for (const theme of ['light', 'dark']) {
      await stills(browser, theme)
      await narrow(browser, theme)
    }
  } finally {
    await browser.close()
  }
  console.log('wrote', OUT)
}

main().catch((err) => { console.error(err); process.exit(1) })
