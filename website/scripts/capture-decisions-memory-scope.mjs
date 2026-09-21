/**
 * Screenshot harness for the Decisions (Jev) card with the recalled-memory scope.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server, answering
 * every /api/** call from fixtures via Playwright route interception — gateway-free,
 * no kiro-cli, no dashboard auth. Same shape as the sibling
 * `capture-settings-feature-previews.mjs`, narrowed to the one card.
 *
 * Consent is answered ON with the tool-argument scope OFF and the recalled-memory
 * scope OFF, which is the state that shows the most: both scope switches are drawn
 * (they appear only while the main switch is on), each sits at the default every
 * already-consented install is in, and the rewritten egress note above them names all
 * three categories. The card is shot alone rather than the whole pane, so the two
 * switches and the note are legible at GitHub's rendering width.
 *
 * Two frames per theme. The second is the scope write FAILING: the PUT is answered
 * `500`, the recalled-memory switch is clicked, and the card is shot with its own
 * `decisions-memory-text-error` notice open. It is a separate node from the
 * tool-argument switch's notice for a reason worth photographing -- a reader has to be
 * able to tell WHICH of the two writes was refused -- and the switch stays at its old
 * value, because a scope the gateway did not record must not read as granted on screen.
 *
 * Usage: node scripts/capture-decisions-memory-scope.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decisions-memory-scope'
mkdirSync(OUT, { recursive: true })

const ENDPOINT = 'https://api.typesafe.ai/v1/systemone'

/** Consent on, for the address it was given for, with both scopes at their default. */
const CONSENT = {
  enabled: true,
  endpoint: ENDPOINT,
  configured_endpoint: ENDPOINT,
  permits: true,
  tool_args: false,
  memory_text: false,
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({
      viewport: { width: 1400, height: 1200 },
      // 12-13px type renders soft at 1x on GitHub.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme })

    // AFTER the shared stub, so these win: Playwright matches route handlers in
    // reverse registration order, and the stub's own init script clears storage.
    //
    // Dispatched on the METHOD: the GET reports the consented state the first frame is
    // of, and the PUT is refused so the second frame has a real failed write to draw.
    // A refusal is the honest way to produce that notice -- it is what the card does
    // when the gateway will not record the scope, so nothing about the component is
    // stubbed to reach it.
    await page.route('**/api/decisions/consent', route =>
      route.request().method() === 'PUT'
        ? route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: JSON.stringify({ error: 'decisions_consent.json is unreadable', code: 'decisions_consent_corrupt' }),
          })
        : route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(CONSENT),
          }),
    )
    // The FLEET ceiling, which the card reads before it draws anything: the shared
    // stub omits `decisions_enabled` and the card fails closed on absence, so without
    // this override there is no card to photograph. Overriding it is the honest thing
    // rather than a workaround — a machine whose fleet permits the seam is the state
    // this screenshot is of.
    await page.route('**/api/dashboard/config', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          restore_sessions: false,
          restore_window_minutes: 30,
          merge_queued_messages: false,
          widget_density: 'more',
          social_share_enabled: true,
          decisions_enabled: true,
        }),
      }),
    )
    await page.addInitScript(() => {
      localStorage.setItem('mc-dev-mode', '1')
      localStorage.setItem('mc-lang', 'en')
    })

    await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })

    // Wait on the NEW switch specifically: it is the thing being photographed, and
    // waiting on the card would pass on a build that never drew it.
    const memorySwitch = page.getByRole('switch', { name: /snippets of recalled memories/i })
    await memorySwitch.waitFor({ state: 'visible', timeout: 20000 })
    // The card's rise animation, and the consent read settling into both switches.
    await page.waitForTimeout(900)

    // The card itself, found from the switch upward, so the frame is the Decisions
    // card rather than a guessed crop of the pane.
    const card = memorySwitch.locator(
      'xpath=ancestor::*[contains(@class, "rounded")][1]/ancestor-or-self::*[1]',
    )
    const target = (await card.count()) > 0 ? card.first() : memorySwitch
    await target.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await target.screenshot({ path: `${OUT}/decisions-card-${theme}.png` })
    console.log('wrote', `${OUT}/decisions-card-${theme}.png`)

    // The refused scope write. Clicking the switch is the only way in: the notice is
    // drawn from the mutation's own error state, so there is nothing to set directly.
    await memorySwitch.click()
    const notice = page.locator('[data-testid="decisions-memory-text-error"]')
    await notice.waitFor({ state: 'visible', timeout: 20000 })
    // The notice's own entry, and the switch settling back to the recorded value.
    await page.waitForTimeout(600)
    await target.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await target.screenshot({ path: `${OUT}/decisions-card-scope-error-${theme}.png` })
    console.log('wrote', `${OUT}/decisions-card-scope-error-${theme}.png`)

    await context.close()
  }

  await browser.close()
  await new Promise(resolve => srv.close(resolve))
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
