/**
 * Screenshots of the nav pin affordance for Agent Capabilities tabs (issue #8500).
 *
 * Drives the REAL SPA with the backend stubbed, the same way
 * capture-leftnav-footer-border.mjs does, because the thing under review is the
 * left rail itself -- an isolated capture entry would have to re-create the rail
 * and would then prove nothing about the rail App.tsx renders.
 *
 * The assertions are what make these frames evidence rather than decoration.
 * Every scene states the claim it is illustrating and FAILS the run if the DOM
 * does not hold it, so a stub regression or a blank page cannot ship as a
 * screenshot of a working feature. This was not hypothetical: the first attempt
 * at this capture produced four plausible-looking PNGs of an app that had
 * rendered no rail at all.
 *
 * Usage:
 *   npm run build            # serveDist() serves website/dist
 *   node scripts/capture-nav-pin-subitems.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/nav-pin-subitems'

// Five is NAV_PINNED_LIMIT (src/lib/navPinned.ts), so this set is exactly at the
// cap and a sixth tab's control must refuse.
const AT_CAP = [
  'capabilities-steering',
  'capabilities-knowledge',
  'capabilities-crews',
  'capabilities-mcp',
  'capabilities-skills',
]

// Without a slot the chat route's own shell throws on an undefined field
// (TypeError: reading 'startsWith') and the ErrorBoundary replaces the whole app,
// rail included -- so the rail scenes need one even though chat is not the subject.
const SLOTS = [
  {
    key: 's1',
    title: 'Pinned nav evidence',
    messages: 2,
    running: false,
    agent: 'kirocrew',
    mode: '',
    created: '2026-09-06T01:00:00Z',
    last_ts: '2026-09-06T04:00:00Z',
    folder_id: '',
  },
]

const RAIL_CLIP = { x: 0, y: 0, width: 300, height: 940 }
const HEADER_CLIP = { x: 300, y: 0, width: 1100, height: 132 }

const SCENES = [
  {
    name: 'rail-1-nothing-pinned',
    claim: 'a capability tab is NOT on the rail until it is pinned',
    pinned: [],
    url: '/chat',
    clip: RAIL_CLIP,
    assert: async page => {
      const n = await page.locator('[data-onboarding-nav^="capabilities-"]').count()
      return n === 0 ? null : `${n} promoted row(s) present with nothing pinned`
    },
  },
  {
    name: 'rail-2-steering-pinned',
    claim: 'pinning Steering files puts it on the rail, in the Main group',
    pinned: ['capabilities-steering'],
    url: '/chat',
    clip: RAIL_CLIP,
    assert: async page => {
      const row = page.locator('[data-onboarding-nav="capabilities-steering"]')
      if ((await row.count()) !== 1) return 'the pinned row is absent from the rail'
      if (!(await row.first().isVisible())) return 'the pinned row is present but not visible'
      return null
    },
  },
  {
    name: 'rail-3-steering-pinned-light',
    claim: 'the promoted row reads in the light theme too',
    pinned: ['capabilities-steering'],
    url: '/chat',
    theme: 'light',
    themeAttr: 'kiro-light',
    clip: RAIL_CLIP,
    assert: async page =>
      (await page.locator('[data-onboarding-nav="capabilities-steering"]').count()) === 1
        ? null
        : 'the pinned row is absent in the light theme',
  },
  {
    name: 'rail-4-collapsed',
    claim: 'on the icon-only rail the promoted row keeps its own glyph',
    pinned: ['capabilities-steering'],
    url: '/chat',
    nav: '1',
    clip: { x: 0, y: 0, width: 160, height: 940 },
    assert: async page => {
      const row = page.locator('[data-onboarding-nav="capabilities-steering"]')
      if ((await row.count()) !== 1) return 'the pinned row is absent from the collapsed rail'
      // Prove the rail really is collapsed rather than trusting the mc-nav seed:
      // a wide rail here would make this frame a duplicate of rail-2.
      const w = await row.first().evaluate(el => {
        const col = el.closest('[class*="w-"]') ?? el.parentElement
        return col ? col.getBoundingClientRect().width : 999
      })
      return w < 140 ? null : `rail is ${Math.round(w)}px wide, so it is not collapsed`
    },
  },
  {
    name: 'rail-5-active-on-its-tab',
    claim: 'the promoted row lights while its own tab is showing',
    pinned: ['capabilities-steering'],
    url: '/capabilities?tab=steering',
    clip: RAIL_CLIP,
    assert: async page => {
      const lit = await page
        .locator('[data-onboarding-nav="capabilities-steering"] .is-lit')
        .count()
      if (lit === 0) return 'the promoted row is not lit while its tab is showing'
      // Counting the WHOLE rail, not just this row: the first version asserted
      // `lit > 0` on one row and therefore passed a rail that lit the promoted
      // row AND its host at once, which is what the frame actually showed.
      const total = await page.locator('[data-onboarding-nav] .is-lit').count()
      return total === 1 ? null : `${total} rail rows read as current, expected exactly 1`
    },
  },
  {
    name: 'header-1-unpinned',
    claim: 'the control offers to pin the tab you are looking at',
    pinned: [],
    url: '/capabilities?tab=steering',
    clip: HEADER_CLIP,
    assert: async page => {
      const b = page.locator('[data-testid="pin-surface-button"]')
      if ((await b.count()) !== 1) return 'no pin control in the Capabilities header'
      if ((await b.getAttribute('aria-pressed')) !== 'false') return 'control reads as pinned'
      if (await b.isDisabled()) return 'control is disabled with nothing pinned'
      return null
    },
  },
  {
    name: 'header-2-pinned',
    claim: 'once pinned the same control offers to unpin',
    pinned: ['capabilities-steering'],
    url: '/capabilities?tab=steering',
    clip: HEADER_CLIP,
    assert: async page => {
      const b = page.locator('[data-testid="pin-surface-button"]')
      return (await b.getAttribute('aria-pressed')) === 'true'
        ? null
        : 'control does not read as pinned'
    },
  },
  {
    name: 'header-4-default-tab-no-param',
    claim: 'the landing view of /capabilities carries no ?tab= yet still offers the control',
    pinned: [],
    url: '/capabilities',
    clip: HEADER_CLIP,
    assert: async page => {
      // No frame covered this URL, which is why the control could go missing on
      // the most prominent sub-item without any screenshot showing it. On desktop
      // SidePanelLayout expresses "first tab" as the ABSENCE of the param, so this
      // is the landing view a user actually meets first.
      const b = page.locator('[data-testid="pin-surface-button"]')
      if ((await b.count()) !== 1) return 'no pin control on the param-less landing view'
      if (!(await b.isVisible())) return 'the control is in the DOM but not visible'
      if ((await b.getAttribute('aria-pressed')) !== 'false') return 'control reads as pinned'
      return null
    },
  },
  {
    name: 'header-3-at-cap',
    claim: 'at the five-pin cap a sixth tab cannot be pinned, and the control says so by being disabled',
    pinned: AT_CAP,
    url: '/capabilities?tab=workflows',
    clip: HEADER_CLIP,
    assert: async page => {
      const b = page.locator('[data-testid="pin-surface-button"]')
      if ((await b.getAttribute('aria-pressed')) !== 'false') return 'the sixth tab reads as pinned'
      if (!(await b.isDisabled())) return 'the control is still enabled at the cap'
      // The tooltip is the part a frame cannot show, so assert it here: a disabled
      // control that still promises "Pin X to the sidebar" is what the blind read
      // misjudged as live.
      const title = (await b.getAttribute('title')) ?? ''
      if (/^Pin /.test(title)) return `at the cap the label still promises the pin: ${title}`
      if (!title.includes('5')) return `the at-cap label does not state the limit: ${title}`
      return null
    },
  },
]

mkdirSync(OUT, { recursive: true })

async function main() {
  // The stub answers '**/api/**', and `src/api/` holds ten real modules -- so
  // against a vite DEV server the stub serves JSON in place of those module
  // scripts and the app never boots. Serving the built bundle is what makes the
  // stub viable, which is why every harness in this folder does it.
  const { srv, base } = await serveDist()
  // --no-sandbox: this host restricts unprivileged user namespaces, so Chromium
  // refuses to start otherwise ("No usable sandbox!"). The page is our own
  // bundle on loopback.
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  let failed = 0
  try {
    for (const s of SCENES) {
      const themeAttr = s.themeAttr ?? 'kiro-dark'
      const ctx = await browser.newContext({
        viewport: { width: 1400, height: 940 },
        deviceScaleFactor: 2,
        colorScheme: s.theme ?? 'dark',
      })
      const page = await ctx.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        theme: s.theme ?? 'dark',
        slots: SLOTS,
        // Seeded through the stub's own init script: registering a second
        // addInitScript would race its localStorage.clear().
        localStorageEntries: {
          'mc-color-theme': themeAttr,
          'mc-privacy-notice-v1': '1',
          'mc-nav': s.nav ?? '0',
          'mc-nav-pinned': JSON.stringify(s.pinned),
        },
      })

      await page.goto(base + s.url, { waitUntil: 'domcontentloaded' })
      try {
        await page.waitForFunction(
          t => document.documentElement.getAttribute('data-theme') === t,
          themeAttr,
          { timeout: 20000 },
        )
        // The rail is the subject of every scene, so wait for a row that exists
        // in all of them rather than for one under assertion.
        await page.locator('[data-onboarding-nav="chat"]').first().waitFor({ timeout: 20000 })
      } catch {
        console.error(`  FAIL ${s.name}: the app never rendered its rail`)
        failed += 1
        await ctx.close()
        continue
      }
      // Park the pointer clear of the rail so no row is hover-lit, which would
      // make the active-state frame ambiguous.
      await page.mouse.move(1200, 600)
      await page.waitForTimeout(600)

      const problem = await s.assert(page)
      if (problem) {
        console.error(`  FAIL ${s.name}: ${problem}`)
        failed += 1
        await ctx.close()
        continue
      }

      await page.screenshot({ path: `${OUT}/${s.name}.png`, clip: s.clip })
      console.log(`  ${s.name} -> ${s.claim}`)
      await ctx.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  if (failed) {
    console.error(`${failed} scene(s) failed -- no frame is trustworthy, not shipping these`)
    process.exit(1)
  }
}

main()
