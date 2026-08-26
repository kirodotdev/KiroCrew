import { test, expect } from '@playwright/test'

/**
 * Both motion paths of the overlay primitives, in one place.
 *
 * The suite's default is the ANIMATED path, because that is what a real user
 * gets. This spec is the only place the reduced-motion branch is exercised.
 * `src/index.css` collapses `animation-duration` to 0.01ms behind
 * `prefers-reduced-motion: reduce`; an app-wide `!important` override like that
 * can freeze motion that carries information rather than decoration (spinners,
 * skeletons) while every other test still passes, so the branch needs a test
 * that reads it directly — and the suite as a whole must NOT be pinned inside
 * it, or the default path would have no coverage at all.
 *
 * The media query is emulated per test with `page.emulateMedia` rather than the
 * `reducedMotion` fixture option: the runner drops that option when it builds
 * the context for the `page` fixture (microsoft/playwright#42001, fixed in
 * 1.63), so the option is silently inert on the version this repo pins.
 */

const HEADER_MENU = 'button[aria-haspopup="menu"][aria-label="More options"]'

/** Seconds in a computed `animation-duration` (e.g. '0.15s', '0.00001s'). */
function seconds(computed: string): number {
  return parseFloat(computed)
}

for (const reducedMotion of ['no-preference', 'reduce'] as const) {
  test(`overlay animation honours prefers-reduced-motion=${reducedMotion}`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion })
    await page.addInitScript(() => {
      window.localStorage.setItem('mc-onboarded', '1')
    })
    await page.goto('/chat')

    // Guard the emulation itself: the fixture option for this is inert (see
    // the file header), so a spec that assumed it would pass vacuously.
    const matches = await page.evaluate(
      () => matchMedia('(prefers-reduced-motion: reduce)').matches
    )
    expect(matches).toBe(reducedMotion === 'reduce')

    const trigger = page.locator(HEADER_MENU).first()
    await trigger.waitFor({ timeout: 15_000 })
    await trigger.click()

    const menu = page.locator('[role="menu"]').first()
    await menu.waitFor({ timeout: 10_000 })
    const duration = seconds(await menu.evaluate(el => getComputedStyle(el).animationDuration))

    if (reducedMotion === 'reduce') {
      // The override applies: 0.01ms, i.e. well under a frame.
      expect(duration).toBeLessThan(0.001)
    } else {
      // The default path really animates. `tailwindcss-animate`'s `animate-in`
      // carries a 150ms duration; asserting a floor rather than the exact value
      // keeps this from breaking on a deliberate timing change.
      expect(duration).toBeGreaterThanOrEqual(0.1)
    }
  })
}
