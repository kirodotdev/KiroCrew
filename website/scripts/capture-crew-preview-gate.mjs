/**
 * Screenshot harness for "crew is hidden until the operator opts in".
 *
 * The change is a VISIBILITY change, so the evidence has to be a pair: the same
 * two surfaces with the flag off and with it on. A single frame proves nothing
 * here — an absent rail row is indistinguishable from a harness that failed to
 * boot, which is why every shot below is taken next to an assertion that names
 * what must be present in that state.
 *
 *   01/04  the sidebar: the `Crew` rail row and the create menu's
 *          "New Crew Mode chat" entry, both absent at 01 and present at 04.
 *   02/03  Developer > Feature Previews: the crew card, off then on. The `on`
 *          frame is taken on the SAME page as the `off` one, so the rail row
 *          appearing behind the card is the evidence that the flip lands live
 *          with no reload — and the rail row is the whole ingress, since this
 *          card deliberately carries no link of its own (the webhooks card
 *          needs one only because `/webhooks` is `hiddenFromNav`).
 *
 * The rail row is located by its registry `navId` (`[data-onboarding-nav]`)
 * rather than by its label: the label is translated, so an English-only locator
 * would report a false "hidden" in every other locale and this harness would
 * then certify the gate from the one case that cannot fail.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures — gateway-free, no
 * kiro-cli, no dashboard token.
 *
 * Usage: node scripts/capture-crew-preview-gate.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-preview-gate'
mkdirSync(OUT, { recursive: true })

/** The localStorage key the Developer > Feature Previews toggle writes. */
const PREVIEW_CREW = 'mc-preview-crew'

/**
 * The create caret's accessible name, read from the English catalog.
 *
 * `fileURLToPath`, not `.pathname`: the latter yields `/C:/…` on Windows and
 * leaves percent-encoding in place, so the read fails there.
 */
const CREATE_MENU_LABEL = (() => {
  const en = JSON.parse(readFileSync(fileURLToPath(new URL('../src/i18n/locales/en.json', import.meta.url)), 'utf-8'))
  const label = en?.pages?.chatSidebar?.more_create_options
  if (!label) throw new Error('catalog key pages.chatSidebar.more_create_options is missing')
  return label
})()

/**
 * One ordinary session rather than an empty list: booting the built SPA against
 * `slots: []` throws in the app shell and renders the error boundary instead of
 * the sidebar, so there would be no menu to open. Pre-existing and unrelated to
 * this gate; every harness in this folder seeds at least one slot.
 */
const SLOTS = [{
  key: 'chat-a', title: 'Draft the release notes', running: false, messages: 4,
  agent: 'kirocrew', mode: '', modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-30T00:10:00Z', folder_id: '', last_message: 'Grouped the entries by area.',
}]

const DETAIL = { running: false, has_more: false, total: 0, queue: [], messages: [] }

/** Config the Developer page's own tabs read; without it the pane throws. */
const extra = async (path, route) => {
  if (path.startsWith('/api/chat/slots/')) { await json(route, DETAIL); return true }
  if (path === '/api/config/kirocrew') {
    await json(route, {
      agents: { kirocrew: { kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' } },
      default_agent: 'kirocrew',
      agent: { default_agent: 'kirocrew', provider: 'acp', model: 'auto' },
      session: { timeout_secs: 1800, pool_size: 2 },
    })
    return true
  }
  if (path === '/api/agent/config') { await json(route, { name: 'kirocrew', mcpServers: {} }); return true }
  return false
}

async function newPage(browser, { crewOn }) {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots: SLOTS,
    theme: 'dark',
    extra,
    // Developer Mode is what puts the Developer row in the rail at all; the
    // preview flag is seeded here (not clicked) for the `on` passes so the
    // sidebar renders its opted-in state on first paint.
    localStorageEntries: {
      'mc-dev-mode': '1',
      ...(crewOn ? { [PREVIEW_CREW]: '1' } : {}),
    },
  })
  return page
}

/** Open the create menu.
 *
 * The caret's accessible name is translated, so it is read from the CATALOG
 * rather than hardcoded — a renamed key then fails this harness loudly instead
 * of silently opening the wrong menu. Probing by content is not an option here:
 * the sidebar has seven menu triggers, and on the `off` pass the crew entry —
 * the only locale-invariant handle in this menu — is exactly what is absent.
 */
async function openCreateMenu(page) {
  const caret = page.getByRole('button', { name: CREATE_MENU_LABEL, exact: true })
  if (await caret.count() === 0) {
    throw new Error(`no create caret named ${JSON.stringify(CREATE_MENU_LABEL)} — did the SPA boot?`)
  }
  await caret.first().click()
  const menu = page.locator('[role="menu"]').filter({ has: page.locator('[role="menuitem"]') }).first()
  await menu.waitFor({ state: 'visible', timeout: 5000 })
  await settle(page, menu)
  return menu
}

/**
 * Wait out `DropdownMenuContent`'s enter animation.
 *
 * `waitFor({ state: 'visible' })` resolves on its FIRST frame, so a shot taken
 * there catches a 95%-scaled, part-transparent menu.
 */
async function settle(page, el) {
  await el.evaluate(node => Promise.all(
    node.getAnimations({ subtree: true }).map(a => a.finished.catch(() => {})),
  ))
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const wrote = []
  try {
    for (const crewOn of [false, true]) {
      const state = crewOn ? 'on' : 'off'

      // ── Sidebar: rail row + create menu ──────────────────────────────────
      const page = await newPage(browser, { crewOn })
      await page.goto(base, { waitUntil: 'networkidle' })
      const railRow = page.locator('[data-onboarding-nav="members"]')
      const railCount = await railRow.count()
      if (crewOn !== (railCount > 0)) {
        throw new Error(`crew ${state}: expected the members rail row ${crewOn ? 'present' : 'absent'}, found ${railCount}`)
      }
      // The ungated neighbour, asserted on BOTH passes: it is what proves the
      // rail rendered at all, so an absent crew row cannot be a dead boot.
      if (await page.locator('[data-onboarding-nav="artifacts"]').count() === 0) {
        throw new Error(`crew ${state}: the rail did not render (no artifacts row)`)
      }
      const menu = await openCreateMenu(page)
      const crewItems = await menu.locator('[data-testid="new-crew-chat"]').count()
      if (crewOn !== (crewItems > 0)) {
        throw new Error(`crew ${state}: expected the crew menu entry ${crewOn ? 'present' : 'absent'}, found ${crewItems}`)
      }
      const name = crewOn ? '04-sidebar-crew-on' : '01-sidebar-crew-off'
      await page.screenshot({ path: `${OUT}/${name}.png` })
      wrote.push(`${name}.png`)
      console.log(`${name}: members rail rows=${railCount} · crew menu entries=${crewItems}`)
      await page.close()
    }

    // ── Developer > Feature Previews: the card, off then on ────────────────
    // One page for both frames, driven by the TOGGLE rather than by seeded
    // storage: what this pair has to show is that clicking the switch is what
    // puts the rail row back, live, which a reload from seeded state cannot.
    const dev = await newPage(browser, { crewOn: false })
    await dev.goto(base + '/developer?tab=feature-previews', { waitUntil: 'domcontentloaded' })
    const crewToggle = dev.getByRole('switch', { name: /^crew$/i })
    await crewToggle.waitFor({ state: 'visible', timeout: 15000 })
    await dev.waitForTimeout(500) // the card's rise animation
    if (await dev.locator('[data-onboarding-nav="members"]').count() !== 0) {
      throw new Error('feature-previews off: the members rail row is present with the switch off')
    }
    await dev.screenshot({ path: `${OUT}/02-feature-previews-crew-off.png` })
    wrote.push('02-feature-previews-crew-off.png')

    await crewToggle.click()
    // The rail row IS the ingress — the card deliberately carries no link of its
    // own (unlike webhooks, which is `hiddenFromNav` and has no rail row at all).
    // Waiting on the row is therefore both the assertion and the evidence: it
    // proves the flip lands live, with no reload.
    await dev.locator('[data-onboarding-nav="members"]').first()
      .waitFor({ state: 'visible', timeout: 5000 })
    const stored = await dev.evaluate(k => localStorage.getItem(k), PREVIEW_CREW)
    if (stored !== '1') throw new Error(`feature-previews on: ${PREVIEW_CREW} is ${stored}, not "1"`)
    await dev.waitForTimeout(300)
    await dev.screenshot({ path: `${OUT}/03-feature-previews-crew-on.png` })
    wrote.push('03-feature-previews-crew-on.png')
    console.log(`feature-previews: toggle wrote ${PREVIEW_CREW}=1 and the rail row returned live`)
    await dev.close()

    console.log(`\nOK — ${wrote.length} shot(s) in ${OUT}: ${wrote.join(', ')}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
