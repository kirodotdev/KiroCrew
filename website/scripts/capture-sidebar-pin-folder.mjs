/**
 * Screenshot harness for the folder pin.
 *
 * The claim is a filter bypass, so a still of a quiet sidebar proves nothing:
 * the evidence is a NARROWED list in which the pinned folder's sessions are
 * still there while the plain folder's are not. Six stories, each in dark and
 * light:
 *
 *   1. The Running chip is on. "oncall" is pinned: both of its idle sessions
 *      stay listed and the header carries the pin glyph; "research" keeps only
 *      its running session.
 *   2. The folder header menu, open on the pinned folder: Unpin folder is there
 *      and Hide folder is present but disabled, with its reason as a muted line
 *      under the label.
 *   3. The sort-and-filter menu: the checkbox row for the pinned folder reads
 *      checked and inert, the pin in the checkbox slot instead of a tick.
 *   4. "ops" is unchecked in the filter menu and holds the pinned "oncall": its
 *      block stays as the container of the pinned child's, whose sessions are
 *      listed, while the uncheck takes "ops"'s OWN session and the sibling
 *      "research" away.
 *   5. The folder header menu, open on the UNPINNED "research": Pin folder is
 *      the item on offer (the other half of story 2).
 *   6. The sort-and-filter menu on the nested tree: "ops" reads checked and
 *      inert because it holds the pinned "oncall", and "oncall" reads checked
 *      and inert because it is pinned; each row carries its reason as a muted
 *      line under its name, so the why is on screen without a hover (a disabled
 *      row takes no pointer events, so its title never shows).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free). Only
 * the network and the localStorage seed are stubbed; the client code under test
 * is unmodified. The harness fails loudly when a story's premise does not hold
 * on screen (a narrowed-away row still present, a pinned row missing).
 *
 * Usage: node scripts/capture-sidebar-pin-folder.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-pin-folder'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const PINNED = 'oncallF'
const PLAIN = 'researchF'

const folders = [
  { id: PINNED, name: 'oncall', order: 0, collapsed: false, pinned: true, color: 'red' },
  { id: PLAIN, name: 'research', order: 1, collapsed: false },
]

/** Story 4: the pinned folder nested under an unchecked parent. */
const PARENT = 'opsF'
const nestedFolders = [
  { id: PARENT, name: 'ops', order: 0, collapsed: false },
  { id: PINNED, name: 'oncall', order: 0, collapsed: false, parent_id: PARENT, pinned: true, color: 'red' },
  { id: PLAIN, name: 'research', order: 1, collapsed: false },
]

const slot = (key, title, folder_id, running, ago) => ({
  key, title, running, messages: 4, agent: 'kirocrew', folder_id,
  modified: now - ago, last_ts: new Date((now - ago) * 1000).toISOString(),
  last_message: running ? 'Reading the alarm history…' : 'Done. Summary posted.',
})

const slots = [
  slot('chat-1-100', 'Sev-2 bridge notes', PINNED, false, 600),
  slot('chat-2-200', 'Pager runbook refresh', PINNED, false, 3600),
  slot('chat-3-300', 'Compare vector stores', PLAIN, true, 60),
  slot('chat-4-400', 'Read the retry RFC', PLAIN, false, 7200),
  slot('chat-5-500', 'Draft the release notes', '', false, 900),
]
const nestedSlots = [...slots, slot('chat-6-600', 'Rota handover', PARENT, false, 1800)]

/** PATCH on a folder answers the merged row, like the real endpoint. */
const extra = (path, route) => {
  const m = path.match(/^\/api\/chat\/folders\/([^/]+)$/)
  if (m && route.request().method() === 'PATCH') {
    const f = folders.find(x => x.id === m[1])
    return json(route, { ...f, ...JSON.parse(route.request().postData() || '{}') }), true
  }
  return false
}

const rect = async locator => {
  const r = await locator.evaluate(el => {
    const b = el.getBoundingClientRect()
    return { x: b.x, y: b.y, width: b.width, height: b.height }
  })
  return r
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })

  let page = null
  async function load(theme, entries, fixture = { slots, folders }) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: fixture.slots, folders: fixture.folders, theme, extra,
      localStorageEntries: {
        'mc-active-slot': 'chat-5-500',
        'mc-privacy-notice-v1': '1',
        'mc-sidebar-pinned': 'true',
        ...entries,
      },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.getByText('Sev-2 bridge notes').first().waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(1200)
  }

  for (const theme of ['dark', 'light']) {
    // 1. Running chip on: the pinned folder keeps its idle rows.
    await load(theme, { 'mc-session-running-only': '1' })
    for (const title of ['Sev-2 bridge notes', 'Pager runbook refresh', 'Compare vector stores']) {
      await page.getByText(title).first().waitFor({ state: 'visible', timeout: 15000 })
    }
    for (const title of ['Read the retry RFC', 'Draft the release notes']) {
      if (await page.getByText(title).count()) throw new Error(`${theme}: "${title}" should be narrowed away by the Running chip`)
    }
    await page.getByTestId(`folder-pinned-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    const sidebar = await rect(page.locator('.sidebar').first())
    await page.screenshot({
      path: `${OUT}/01-running-chip-pinned-folder-${theme}.png`,
      clip: { x: sidebar.x, y: sidebar.y, width: sidebar.width, height: Math.min(sidebar.height, 520) },
    })

    // 2. The folder menu on the pinned folder.
    await page.getByTestId(`folder-menu-${PINNED}`).click()
    await page.getByTestId(`folder-pin-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    const hideItem = page.getByTestId(`folder-visibility-${PINNED}`)
    await hideItem.waitFor({ state: 'visible', timeout: 15000 })
    if ((await hideItem.getAttribute('data-disabled')) === null) {
      throw new Error(`${theme}: Hide folder is live on a pinned folder`)
    }
    await page.getByTestId(`folder-visibility-reason-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    const menu = await rect(page.getByTestId(`folder-pin-${PINNED}`).locator('xpath=ancestor::*[@role="menu"]'))
    await page.screenshot({
      path: `${OUT}/02-folder-menu-unpin-${theme}.png`,
      clip: { x: 0, y: Math.max(0, menu.y - 90), width: Math.min(1400, menu.x + menu.width + 40), height: menu.height + 130 },
    })
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)

    // 3. The filter menu: the pinned folder's checkbox row is inert.
    await page.getByLabel('Sort and filter sessions').click()
    const row = page.getByTestId(`folder-filter-${PINNED}`)
    await row.waitFor({ state: 'visible', timeout: 15000 })
    if ((await row.getAttribute('aria-checked')) !== 'true' || (await row.getAttribute('data-disabled')) === null) {
      throw new Error(`${theme}: the pinned folder's filter row is not checked-and-inert`)
    }
    await page.getByTestId(`folder-filter-lock-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    const menu2 = await rect(row.locator('xpath=ancestor::*[@role="menu"]'))
    await page.screenshot({
      path: `${OUT}/03-filter-menu-pinned-row-${theme}.png`,
      clip: { x: Math.max(0, menu2.x - 20), y: Math.max(0, menu2.y - 20), width: menu2.width + 40, height: Math.min(900 - menu2.y + 20, menu2.height + 40) },
    })
    await page.keyboard.press('Escape')

    // 4. An unchecked parent holding the pinned folder: the parent's block stays
    //    as the container, the pinned child's sessions in it, the parent's own
    //    session gone with its uncheck.
    await load(theme, { 'mc-flat-hidden-folders': JSON.stringify([PARENT, PLAIN]) }, { slots: nestedSlots, folders: nestedFolders })
    for (const title of ['Sev-2 bridge notes', 'Pager runbook refresh']) {
      await page.getByText(title).first().waitFor({ state: 'visible', timeout: 15000 })
    }
    await page.getByTestId(`folder-pinned-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    if (await page.getByText('Rota handover').count()) throw new Error(`${theme}: "Rota handover" should be hidden by the unchecked ops folder`)
    for (const title of ['Compare vector stores', 'Read the retry RFC']) {
      if (await page.getByText(title).count()) throw new Error(`${theme}: "${title}" should be hidden by the unchecked research folder`)
    }
    const sidebar4 = await rect(page.locator('.sidebar').first())
    await page.screenshot({
      path: `${OUT}/04-unchecked-parent-keeps-pinned-child-${theme}.png`,
      clip: { x: sidebar4.x, y: sidebar4.y, width: sidebar4.width, height: Math.min(sidebar4.height, 560) },
    })

    // 5. The folder menu on the UNPINNED folder: Pin folder is on offer. A fresh
    //    load with nothing unchecked: story 4 hid this folder's block, menu included.
    await load(theme, {}, { slots: nestedSlots, folders: nestedFolders })
    await page.getByText('Compare vector stores').first().waitFor({ state: 'visible', timeout: 15000 })
    await page.getByTestId(`folder-menu-${PLAIN}`).click()
    const pinItem = page.getByTestId(`folder-pin-${PLAIN}`)
    await pinItem.waitFor({ state: 'visible', timeout: 15000 })
    if (!/pin folder/i.test(await pinItem.innerText()) || /unpin/i.test(await pinItem.innerText())) {
      throw new Error(`${theme}: the unpinned folder's menu does not offer "Pin folder"`)
    }
    const menu5 = await rect(pinItem.locator('xpath=ancestor::*[@role="menu"]'))
    await page.screenshot({
      path: `${OUT}/05-folder-menu-pin-unpinned-${theme}.png`,
      clip: { x: 0, y: Math.max(0, menu5.y - 90), width: Math.min(1400, menu5.x + menu5.width + 40), height: menu5.height + 130 },
    })
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)

    // 6. The filter menu on the nested tree: the pinned row is inert with the
    //    pin in its checkbox slot and says why under its name; the ancestor
    //    row stays a plain working checkbox (its tick still hides its own
    //    sessions, the pinned block stays as its container), so it carries no
    //    pin and no reason line. A fresh load with nothing unchecked.
    await load(theme, {}, { slots: nestedSlots, folders: nestedFolders })
    await page.getByLabel('Sort and filter sessions').click()
    const pinnedRow = page.getByTestId(`folder-filter-${PINNED}`)
    await pinnedRow.waitFor({ state: 'visible', timeout: 15000 })
    if ((await pinnedRow.getAttribute('aria-checked')) !== 'true' || (await pinnedRow.getAttribute('data-disabled')) === null) {
      throw new Error(`${theme}: the ${PINNED} filter row is not checked-and-inert`)
    }
    await page.getByTestId(`folder-filter-reason-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    await page.getByTestId(`folder-filter-lock-${PINNED}`).waitFor({ state: 'visible', timeout: 15000 })
    for (const id of [PARENT, PLAIN]) {
      const r = page.getByTestId(`folder-filter-${id}`)
      await r.waitFor({ state: 'visible', timeout: 15000 })
      if ((await r.getAttribute('aria-checked')) !== 'true' || (await r.getAttribute('data-disabled')) !== null) {
        throw new Error(`${theme}: the ${id} filter row is not a plain checked checkbox`)
      }
      if (await page.getByTestId(`folder-filter-lock-${id}`).count()) {
        throw new Error(`${theme}: the ${id} filter row carries a pin`)
      }
      if (await page.getByTestId(`folder-filter-reason-${id}`).count()) {
        throw new Error(`${theme}: the ${id} filter row carries a reason line`)
      }
    }
    const menu6 = await rect(page.getByTestId(`folder-filter-${PARENT}`).locator('xpath=ancestor::*[@role="menu"]'))
    await page.screenshot({
      path: `${OUT}/06-filter-menu-holds-pinned-row-${theme}.png`,
      clip: { x: Math.max(0, menu6.x - 20), y: Math.max(0, menu6.y - 20), width: menu6.width + 40, height: Math.min(900 - menu6.y + 20, menu6.height + 40) },
    })
    await page.keyboard.press('Escape')
    console.log(`${theme}: six stories captured`)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
