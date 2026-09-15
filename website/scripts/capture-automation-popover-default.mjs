/**
 * Screenshot harness for WHICH VIEW the composer's automation popover opens on.
 *
 * The popover holds two surfaces: a bounded pull-request monitor (one target,
 * validated against four code hosts) and the goal loop (any objective). Only
 * one of them can be the default, and the choice is visible nowhere except the
 * rendered panel — which is why this exists rather than a DOM test alone.
 *
 * It ASSERTS as well as photographs, against the REAL built SPA in
 * website/dist: it fails if the panel that opens is not the goal editor, if the
 * pull-request-only field is on it, or if the offer to switch is missing. So a
 * regression back to a PR-only default is a non-zero exit, not a photo nobody
 * compares.
 *
 * Nothing in CI runs this file; it is a manual guard and the source of the PR's
 * evidence. The CI-enforced half is src/test/SessionAutomationPopover.test.tsx.
 *
 * Gateway-free: serveDist + stubDashboardApi, so it never dials a live
 * instance and needs no token.
 *
 * To photograph the PR-only default for a before/after pair, check the two
 * components out at a ref that predates this change
 * (`git checkout <ref> -- src/components/SessionAutomationPopover.tsx src/components/AutoNudgePopover.tsx`),
 * `npm run build`, and run this with a different outDir and --expect-bounded.
 *
 * Usage: node scripts/capture-automation-popover-default.mjs [outDir] [--expect-bounded]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2).filter(a => a !== '--expect-bounded')
const EXPECT_BOUNDED = process.argv.includes('--expect-bounded')
const OUT = args[0] || '../temp-screenshots/automation-popover-default'
const SLOT = 'chat-monitor'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Trim the flaky-test backlog',
  running: false,
  last_message: 'Two candidates left.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Which flaky test should we take first?' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Two candidates left.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The panel is 11-12px type; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
    /* Every assertion below addresses a control by its ENGLISH accessible name,
       so the render has to be English for those lookups to mean anything. Two
       pins, because the language is decided twice: this one fixes what the
       browser reports, and `mc-lang` below fixes what the app resolves from it.
       Without them the harness inherits the operator's locale -- a German dev
       machine renders German labels and the script exits 1 reporting a missing
       trigger, which reads as the defect it exists to detect. */
    locale: 'en-US',
  })

  /**
   * Nothing is armed on this slot, which is the state the default view is a
   * question about. Each branch awaits `json()` and returns true, because the
   * shared stub treats a falsy return as "not handled" and fulfils the route
   * itself -- returning `json(...)` alone double-fulfils.
   */
  const extra = async (path, route) => {
    if (path === `/api/autonudge/slot/${SLOT}`) {
      await json(route, { enabled: true, loop: null })
      return true
    }
    if (path === `/api/monitors/slot/${SLOT}`) {
      await json(route, { enabled: true, monitor: null })
      return true
    }
    if (path === '/api/monitors') { await json(route, { enabled: true, monitors: [] }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  const results = []

  async function shoot(name, theme) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme,
      extra,
      /* The stub's own init script clears storage and seeds only the theme, so
         `mc-lang` has to be passed HERE rather than through a second
         `addInitScript`: Playwright does not order separately registered init
         scripts, and one racing the stub's clear would leave the language
         unset again. Unset means `resolveLanguage` falls through to the
         browser, which is the other half of what `locale` above pins. */
      localStorageEntries: { 'mc-lang': 'en' },
    })
    await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    /* Addressed by accessible name, which is itself part of what changed: the
       button named the bounded monitor while that was the default. */
    const triggerName = EXPECT_BOUNDED ? 'Set up a bounded monitor' : 'Set a goal'
    const trigger = page.getByRole('button', { name: triggerName }).first()
    const found = await trigger.count().then(n => n > 0).catch(() => false)
    if (!found) {
      results.push({ name, ok: false, why: `composer trigger "${triggerName}" not found` })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      await page.close()
      return
    }
    await trigger.click()
    await page.waitForTimeout(700)

    const panel = page.locator('[data-side]').first()
    const box = await panel.boundingBox().catch(() => null)
    if (!box) {
      results.push({ name, ok: false, why: 'popover panel did not render' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      await page.close()
      return
    }

    const has = async (role, label) =>
      await page.getByRole(role, { name: label }).first().count().then(n => n > 0).catch(() => false)

    const goalField = await has('textbox', 'Goal description')
    const prField = await has('textbox', 'Pull request URL')
    const offer = await has('button', 'Watch a pull request instead')

    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, box.x - 12),
        y: Math.max(0, box.y - 12),
        width: box.width + 24,
        height: box.height + 24,
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)

    const ok = EXPECT_BOUNDED
      ? (prField && !goalField)
      : (goalField && !prField && offer)
    results.push({ name, ok, goalField, prField, offerToSwitch: offer })
    await page.close()
  }

  await shoot('01-default-view-dark', 'dark')
  await shoot('02-default-view-light', 'light')

  await browser.close()
  srv.close()

  const want = EXPECT_BOUNDED
    ? 'the pull-request form (pre-change default)'
    : 'the goal editor, with no pull-request field and an offer to switch'
  console.log(`--- assertions (the popover must open on ${want}) ---`)
  for (const r of results) console.log(JSON.stringify(r))

  if (!results.every(r => r.ok)) {
    console.error('FAIL: the popover did not open on the expected view')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
