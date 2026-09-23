/**
 * Screenshots of the Crew Members rail glyph — the two-ghost pair brand mark
 * that replaced the Lucide `Users` icon on `/members`.
 *
 * Drives the REAL SPA with the backend stubbed, the same way
 * capture-nav-pin-subitems.mjs does, because the subject IS the left rail: an
 * isolated capture entry would have to re-create the rail and would then prove
 * nothing about the rail App.tsx renders — and specifically nothing about how the
 * new glyph sits NEXT TO its Lucide neighbours, which is the whole question.
 *
 * The claim under test is stroke parity: a masked asset is not a Lucide
 * component, so nothing in the type system or the test suite forces it to carry
 * the same optical weight as the glyphs above and below it. Every scene asserts
 * the DOM holds what the frame appears to show and FAILS the run otherwise, so a
 * stub regression or an unrendered rail cannot ship as evidence of a working
 * icon. `glyph-strip` is captured at deviceScaleFactor 8 so
 * `measure-glyph-stroke.py` can measure the rendered stroke in device pixels
 * instead of leaving "same weight" as an eyeball judgement.
 *
 * Usage:
 *   npm run build            # serveDist() serves website/dist
 *   node scripts/capture-member-nav-glyph.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/member-nav-glyph'

// Without a slot the chat route's own shell throws on an undefined field and the
// ErrorBoundary replaces the whole app, rail included — so the rail scenes need
// one even though chat is not the subject.
const SLOTS = [
  {
    key: 's1',
    title: 'Crew Member icon evidence',
    messages: 2,
    running: false,
    agent: 'kirocrew',
    mode: '',
    created: '2026-09-16T01:00:00Z',
    last_ts: '2026-09-16T04:00:00Z',
    folder_id: '',
  },
]

const RAIL_CLIP = { x: 0, y: 0, width: 300, height: 620 }

/** The rail row whose glyph this change replaces. */
const MEMBERS_ROW = '[data-onboarding-nav="members"]'
/** Its nearest neighbour, and the reference weight: a Lucide glyph at size 16. */
const CHAT_ROW = '[data-onboarding-nav="chat"]'

/**
 * The glyph is a masked <span>, not an <svg>, so "is the icon there" and "is it
 * the RIGHT icon" are different questions. Assert both, plus the two properties
 * that make the mask follow the rail's colour states — a mask that lost its
 * `currentColor` background paints as a solid square and still screenshots as
 * "an icon".
 */
async function assertMemberGlyph(page) {
  const row = page.locator(MEMBERS_ROW)
  if ((await row.count()) !== 1) return 'no Crew Members row on the rail'
  if (!(await row.first().isVisible())) return 'the Crew Members row is present but not visible'

  const glyph = row.first().locator('[data-testid="crew-member-mark"]')
  if ((await glyph.count()) !== 1) return 'the Crew Members row does not render the crew-member mark'

  // A leftover Lucide glyph would mean the swap silently did not take.
  if ((await row.first().locator('svg').count()) > 0) {
    return 'the Crew Members row still renders an <svg> glyph'
  }

  const box = await glyph.first().evaluate(el => {
    const s = getComputedStyle(el)
    return {
      w: el.getBoundingClientRect().width,
      h: el.getBoundingClientRect().height,
      bg: s.backgroundColor,
      mask: s.maskImage || s.webkitMaskImage,
      size: s.maskSize || s.webkitMaskSize,
    }
  })
  if (Math.round(box.w) !== 16 || Math.round(box.h) !== 16) {
    return `glyph box is ${box.w}x${box.h}, not the 16x16 its Lucide neighbours use`
  }
  // Any resolved, non-transparent paint counts. Deliberately not an `/^rgb/`
  // test: the light theme's tokens resolve through `color(srgb …)`, so matching
  // on the serialization form fails a perfectly good glyph (it did, on the first
  // run of this harness) while proving nothing extra.
  const transparent = !box.bg || box.bg === 'transparent' || /^rgba\(0,\s*0,\s*0,\s*0\)$/.test(box.bg)
  if (transparent) return `glyph has no painted fill to tint: ${box.bg}`
  if (!box.mask || box.mask === 'none') return 'glyph carries no mask image (it would paint as a solid square)'
  if (!/contain/.test(box.size)) return `mask-size is ${box.size}, not contain`
  return null
}

const SCENES = [
  {
    name: 'rail-dark',
    claim: 'the Crew Members row draws the two-ghost pair mark, at the same weight as the Lucide rows around it',
    theme: 'dark',
    themeAttr: 'kiro-dark',
    clip: RAIL_CLIP,
    assert: assertMemberGlyph,
  },
  {
    name: 'rail-light',
    claim: 'the same mark inverts with the theme, which a PNG could not do',
    theme: 'light',
    themeAttr: 'kiro-light',
    clip: RAIL_CLIP,
    assert: assertMemberGlyph,
  },
  {
    name: 'rail-dark-active',
    claim: 'on the active row the mark takes the accent colour, like a Lucide glyph',
    theme: 'dark',
    themeAttr: 'kiro-dark',
    url: '/members',
    clip: RAIL_CLIP,
    assert: async page => {
      const problem = await assertMemberGlyph(page)
      if (problem) return problem
      // Active-state proof: the glyph's own painted colour must differ from an
      // idle row's. Equal colours would mean the mark ignores selection — the
      // exact failure a fixed-colour <img> or PNG would have.
      const paint = sel =>
        page
          .locator(`${sel} [data-testid="crew-member-mark"], ${sel} svg`)
          .first()
          .evaluate(el => getComputedStyle(el).color)
      const active = await paint(MEMBERS_ROW)
      const idle = await paint(CHAT_ROW)
      return active === idle ? `active and idle rows paint the same colour (${active})` : null
    },
  },
  {
    // Deliberately NOT clipped to the rail: this is the measurement frame, so it
    // holds the new mark and a known-Lucide glyph in one image at a scale where
    // a stroke is many pixels wide.
    name: 'glyph-strip',
    claim: 'at 8x the new mark and the Lucide glyph above it stroke to the same width',
    theme: 'dark',
    themeAttr: 'kiro-dark',
    scale: 8,
    // A THIRD route, so neither compared row is the active one. On /chat the
    // Sessions glyph paints in accent while Crew Members paints muted, and a
    // measurement across two different contrasts is not a measurement of weight.
    url: '/notifications',
    assert: assertMemberGlyph,
    clipFrom: async page => {
      const a = await page.locator(`${CHAT_ROW} svg`).first().boundingBox()
      const b = await page.locator(`${MEMBERS_ROW} [data-testid="crew-member-mark"]`).first().boundingBox()
      const pad = 4
      const x = Math.min(a.x, b.x) - pad
      const y = Math.min(a.y, b.y) - pad
      return {
        x,
        y,
        width: Math.max(a.x + a.width, b.x + b.width) - x + pad,
        height: Math.max(a.y + a.height, b.y + b.height) - y + pad,
      }
    },
  },
]

mkdirSync(OUT, { recursive: true })

async function main() {
  // The stub answers '**/api/**' and `src/api/` holds real modules, so against a
  // vite DEV server the stub would serve JSON in place of module scripts and the
  // app would never boot. Serving the built bundle is what makes the stub viable.
  const { srv, base } = await serveDist()
  // --no-sandbox: this host restricts unprivileged user namespaces, so Chromium
  // refuses to start otherwise. The page is our own bundle on loopback.
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  let failed = 0
  try {
    for (const s of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: 1400, height: 940 },
        deviceScaleFactor: s.scale ?? 2,
        colorScheme: s.theme,
      })
      const page = await ctx.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        theme: s.theme,
        slots: SLOTS,
        // Seeded through the stub's own init script: addInitScript would race
        // its localStorage.clear().
        localStorageEntries: {
          'mc-color-theme': s.themeAttr,
          'mc-privacy-notice-v1': '1',
          'mc-nav': '0',
          // The Crew Members surface is preview-gated, so without this the row
          // it is the subject of is simply absent from the rail.
          'mc-preview-crew': '1',
        },
      })

      await page.goto(base + (s.url ?? '/chat'), { waitUntil: 'domcontentloaded' })
      try {
        await page.waitForFunction(
          t => document.documentElement.getAttribute('data-theme') === t,
          s.themeAttr,
          { timeout: 20000 },
        )
        await page.locator(CHAT_ROW).first().waitFor({ timeout: 20000 })
        await page.locator(MEMBERS_ROW).first().waitFor({ timeout: 20000 })
      } catch {
        console.error(`  FAIL ${s.name}: the app never rendered its rail`)
        failed += 1
        await ctx.close()
        continue
      }
      // Park the pointer clear of the rail so no row is hover-lit, which would
      // make a colour-state frame ambiguous.
      await page.mouse.move(1200, 600)
      await page.waitForTimeout(600)

      const problem = await s.assert(page)
      if (problem) {
        console.error(`  FAIL ${s.name}: ${problem}`)
        failed += 1
        await ctx.close()
        continue
      }

      const clip = s.clipFrom ? await s.clipFrom(page) : s.clip
      await page.screenshot({ path: `${OUT}/${s.name}.png`, clip })
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
