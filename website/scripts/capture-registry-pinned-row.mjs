/**
 * Screenshots of the build-pinned registry row beside an operator-added one.
 *
 * Drives the ISOLATED capture entry (website/capture/registry-pinned-row.html),
 * which mounts RegistryManager against the real stylesheet and theme tokens with
 * `api.listRegistries` stubbed to the `{registries, pinned}` shape the endpoint
 * now returns. The stub replaces the backend, not the component.
 *
 * The assertions below are what make this evidence rather than decoration. The
 * claim under review is that the two row kinds are visibly and functionally
 * different: a pinned row is read-only (badge, no remove control) because a
 * delete there would appear to work and be undone by the next read, while an
 * operator row keeps its remove control. A regression that rendered a remove
 * button on the pinned row, or dropped it from the operator row, fails the run
 * instead of silently producing a misleading frame.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-registry-pinned-row.mjs http://127.0.0.1:6831 ../temp-screenshots/registry-pinned-row
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/registry-pinned-row'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  // At rest the frame shows the BADGE difference; only a hovered row shows the
  // CONTROL difference the PR body claims, and only the narrow scene shows that
  // a touch viewport (no hover) can still reach the controls. Shipping the rest
  // frame alone would assert "no remove control" with an image in which neither
  // row shows any control.
  const SCENES = [
    { scene: 'rest', hover: null, width: 780, note: 'badge marks the pinned row' },
    {
      scene: 'hover-pinned',
      hover: '[aria-label="Refresh Platform Apps registry"]',
      width: 780,
      note: 'hovered pinned row offers open + refresh — no remove',
    },
    {
      scene: 'hover-operator',
      hover: '[aria-label="Remove My Team registry"]',
      width: 780,
      note: 'hovered operator row keeps open/refresh/remove',
    },
    // 320px is the narrowest viewport the repo supports, and the added badge is
    // what pushed the pinned row over. This scene proves the metadata wraps and
    // that the controls no longer depend on hover.
    { scene: 'narrow-320', hover: null, width: 320, note: 'wraps, controls visible at 320px' },
  ]
  for (const theme of ['dark', 'light']) {
    for (const { scene, hover, width, note } of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width, height: 460 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/registry-pinned-row.html?theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      try {
        await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
        await page.waitForSelector('text=Included with this installation', { timeout: 10000 })
      } catch {
        console.error(
          `  FAIL ${theme}/${scene}: the pinned row never rendered` +
            (errors.length ? ` (${errors[0]})` : ''),
        )
        failed += 1
        await ctx.close()
        continue
      }

      // The pinned rows must carry NO remove control; the operator row must keep
      // one. Both are asserted, so a change that stripped the control from every
      // row would fail here rather than pass on the pinned half alone.
      const pinnedRemove = await page.$('[aria-label="Remove Platform Apps registry"]')
      const operatorRemove = await page.$('[aria-label="Remove My Team registry"]')
      if (pinnedRemove !== null) {
        console.error(`  FAIL ${theme}/${scene}: the pinned row rendered a remove control`)
        failed += 1
        await ctx.close()
        continue
      }
      if (operatorRemove === null) {
        console.error(`  FAIL ${theme}/${scene}: the operator row lost its remove control`)
        failed += 1
        await ctx.close()
        continue
      }
      // Opening the repo is read-only, so a pinned row offers it.
      if ((await page.$('[aria-label*="app-registry.git repository"]')) === null) {
        console.error(`  FAIL ${theme}/${scene}: the pinned row lost its open-repository control`)
        failed += 1
        await ctx.close()
        continue
      }

      if (hover) {
        await page.hover(hover)
        // Let the opacity transition settle so the frame shows the controls
        // fully painted rather than mid-fade.
        await page.waitForTimeout(400)
      }

      if (scene === 'narrow-320') {
        // No hover here: the point is that a touch viewport can reach the
        // controls at all. `isVisible` would pass on an opacity-0 element, so
        // assert the computed opacity instead.
        const opacity = await page.$eval(
          '[aria-label="Refresh Platform Apps registry"]',
          el => getComputedStyle(el).opacity,
        )
        if (opacity !== '1') {
          console.error(
            `  FAIL ${theme}/${scene}: the pinned row's refresh is opacity ${opacity} without hover`,
          )
          failed += 1
          await ctx.close()
          continue
        }
      }

      const target = await page.$('[data-capture-root]')
      await target.screenshot({ path: `${OUT}/${theme}-${scene}.png` })
      console.log(`  ${theme}/${scene} -> ${note}`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) {
    console.error(`${failed} scene(s) failed`)
    process.exit(1)
  }
}

run()
