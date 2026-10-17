/**
 * Screenshot harness for the "Split down" entry from the single-session view.
 *
 * Split view opens with the fresh pane beside the session. The session menu
 * behind the desktop title chevron offers "Split down", which opens the stacked
 * layout in one step; the header's split view button already starts the side-by-side
 * one, so that menu has no "Split right" row. The phone's ⋯ menu has no such
 * button, so it lists "Split right" and "Split down" together. The menu rows ARE the
 * change, so the evidence is each menu OPEN and the layout one click opens.
 *
 * Frames (outDir, default ../temp-screenshots/18880-split-down):
 *   01-desktop-title-menu.png     title-chevron menu open: "Split down" right after
 *                                 "Pop out to window", and no "Split right" row
 *   02-phone-more-menu.png        phone ⋯ menu open: "Split right" above "Split down"
 *   03-desktop-stacked-split.png  after the click: the session on top, the picker below
 *   04-phone-stacked-split.png    the same click on a phone
 *
 * The harness asserts what it photographs. On each device a control run enters
 * through the default path (the header button on desktop, "Split right" on the
 * phone) and must land side by side; the "Split down" run must land stacked.
 * Two runs that differ only in the control used make the direction the
 * variable, so a stacked frame cannot come from a layout that is stacked by
 * default.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free).
 *
 * Usage: node scripts/capture-18880-split-down.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'
import { makeChecker } from './lib/prepare-split-chat-page.mjs'

const OUT = process.argv[2] || '../temp-screenshots/18880-split-down'
mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-checklist'
const PICKER = 'Search sessions…'

const slots = [
  {
    key: SLOT, title: 'Release checklist review', messages: 2, running: false,
    agent: 'kirocrew', created: '2026-08-25T09:00:00Z', last_ts: '2026-08-25T10:00:00Z', folder_id: '',
  },
  {
    key: 'chat-triage', title: 'Pipeline triage', messages: 2, running: false,
    agent: 'oncall', created: '2026-08-24T09:00:00Z', last_ts: '2026-08-25T09:30:00Z', folder_id: '',
  },
]

const transcripts = {
  [SLOT]: [
    { role: 'user', content: 'Is the release checklist ready to sign off?', ts: '2026-08-25T09:58:00Z', meta: { mid: 'c-1' } },
    { role: 'assistant', content: 'Every item is checked except the changelog entry. I can draft it once you confirm the version number.', ts: '2026-08-25T09:59:00Z', meta: { mid: 'c-2' } },
  ],
  'chat-triage': [
    { role: 'user', content: 'Anything paging overnight?', ts: '2026-08-25T09:28:00Z', meta: { mid: 't-1' } },
    { role: 'assistant', content: 'Nothing paged. One warning cleared itself at 03:10.', ts: '2026-08-25T09:29:00Z', meta: { mid: 't-2' } },
  ],
}

// Every frame stays at 2000 px or less on a side (the desktop at 1x, the phone at 2x),
// so image readers that cap a side at 2000 px see the frames as captured.
const DEVICES = {
  desktop: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
  phone: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
}

const { check, failed } = makeChecker()

/** A tap on the phone, a click on the desktop. */
const press = (device, locator) => (device === 'phone' ? locator.tap() : locator.click())

/** A fresh page on the single-session view: split view is on and no layout is saved. */
async function openSession(browser, base, device) {
  const context = await browser.newContext(DEVICES[device])
  const page = await context.newPage()
  logPageProblems(page)
  await stubSplitPanes(page, { slots, transcripts, layout: {} })
  await page.goto(`${base}/chat/${SLOT}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('changelog entry').first().waitFor({ timeout: 20_000 })
  return page
}

/** The control that opens the session's menu: the title chevron, or the phone's ⋯.
 *  Four buttons share the label "More actions", so the phone's bar button is found by test id. */
const menuTrigger = (page, device) => (device === 'phone'
  ? page.getByTestId('mobile-topbar-more')
  : page.getByRole('button', { name: 'Session options', exact: true }))

async function openMenu(page, device) {
  const trigger = menuTrigger(page, device)
  check(`${device} menu trigger is unique`, (await trigger.count()) === 1, `count=${await trigger.count()}`)
  await press(device, trigger)
  const menu = page.getByRole('menu')
  await menu.waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(400)
  return menu
}

/** Wait for the picker pane the split opens and return it and the session pane as boxes.
 *  The sidebar's search field shares the placeholder; only the picker's input carries an aria-label. */
async function splitBoxes(page) {
  const picker = page.locator(`input[aria-label="${PICKER}"]`)
  await picker.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(600)
  return {
    session: await page.locator('[data-chat-pane]').first().boundingBox(),
    picker: await picker.locator('xpath=../..').boundingBox(),
  }
}

const besideEachOther = ({ session, picker }) => picker.x >= session.x + session.width - 1
const stacked = ({ session, picker }) => picker.y >= session.y + session.height - 1

/** Control run: a default-direction entry (the header button on desktop, the menu's
 *  "Split right" row on the phone) must open the picker to the right. */
async function controlRun(browser, base, device, entry) {
  const page = await openSession(browser, base, device)
  if (entry === 'menu') {
    const menu = await openMenu(page, device)
    await press(device, menu.getByRole('menuitem', { name: 'Split right', exact: true }))
  } else {
    await page.getByRole('button', { name: 'Enter split view', exact: true }).click()
  }
  check(`${device} ${entry} entry opens side by side`, besideEachOther(await splitBoxes(page)), '')
  await page.context().close()
}

/** Evidence run: the menu OPEN, then the click, then the layout it opens. */
async function splitDownRun(browser, base, device) {
  const page = await openSession(browser, base, device)
  const menu = await openMenu(page, device)
  const items = (await menu.getByRole('menuitem').allInnerTexts()).map((s) => s.trim())
  const at = items.indexOf('Split down')
  check(`${device} menu offers Split down once`, items.filter((s) => s === 'Split down').length === 1, JSON.stringify(items))
  if (device === 'desktop') {
    check('desktop menu has no Split right row', !items.includes('Split right'), JSON.stringify(items))
    check('desktop Split down follows Pop out to window', at > 0 && items[at - 1] === 'Pop out to window', JSON.stringify(items))
  } else {
    check('phone Split right sits directly above Split down', at > 0 && items[at - 1] === 'Split right', JSON.stringify(items))
  }
  const row = menu.getByRole('menuitem', { name: 'Split down', exact: true })
  if (device === 'desktop') await row.hover()
  await page.waitForTimeout(150)
  await page.screenshot({ path: `${OUT}/${device === 'phone' ? '02-phone-more-menu' : '01-desktop-title-menu'}.png` })

  await press(device, row)
  check(`${device} Split down opens stacked`, stacked(await splitBoxes(page)), '')
  await page.screenshot({ path: `${OUT}/${device === 'phone' ? '04-phone-stacked-split' : '03-desktop-stacked-split'}.png` })
  await page.context().close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const device of Object.keys(DEVICES)) {
    await controlRun(browser, base, device, device === 'phone' ? 'menu' : 'button')
    await splitDownRun(browser, base, device)
  }
  await browser.close()
  srv.close()
  if (failed()) {
    console.error('FAIL: a frame does not show what its name says')
    process.exit(1)
  }
  console.log(`OK: four frames written to ${OUT}`)
}

main().catch((err) => { console.error(err); process.exit(1) })
