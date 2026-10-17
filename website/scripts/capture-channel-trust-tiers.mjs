/**
 * Screenshots of the channel approval card's trust tiers (issue #5231).
 *
 * Drives the isolated capture entry (website/capture/channel-trust-tiers.html),
 * which mounts the REAL ApprovalCard + TrustDropdown with the title resolved by
 * the REAL `approvalToolTitle` helper from a backend-shaped approval message.
 * Radix renders the open menu into a portal outside the capture root, so the
 * open-menu frame is a viewport shot rather than an element shot.
 *
 * Each scene ASSERTS the exact-command tier offers the COMMAND, not the agent
 * role — the property this change exists to establish. On the old code the card
 * title was the role ("dev"), so the assertion fails and no misleading frame is
 * written.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-channel-trust-tiers.mjs http://127.0.0.1:6823 ../temp-screenshots/channel-trust-tiers
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/channel-trust-tiers'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'channel-approval-card-dark', theme: 'dark', open: false },
  { name: 'channel-trust-menu-dark', theme: 'dark', open: true },
  { name: 'channel-trust-menu-light', theme: 'light', open: true },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 340 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/channel-trust-tiers.html?theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  let ok = true
  if (s.open) {
    await page.getByRole('button', { name: /Trust/ }).click()
    await page.waitForSelector('[role="menuitem"]')
    // Radix animates the menu in (opacity/scale); a mid-animation frame shows
    // the card bleeding through the menu. Wait for the animation to finish so
    // the frame shows the settled, opaque menu.
    await page.locator('[role="menu"]').first().evaluate(
      el => Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished)),
    )
    const items = await page.locator('[role="menuitem"]').allInnerTexts()
    const exact = (items[0] ?? '').trim()
    // The tier must offer the COMMAND — the agent role means the old
    // (role-titled) card rendered, and its grant could never match.
    const offersCommand = exact.includes('ls -la /workplace/project')
    const offersRole = /\bdev\b/.test(exact)
    const hasBase = items.some(t => /commands/.test(t))
    const hasAll = items.some(t => /Trust all tools/.test(t))
    ok = items.length === 3 && offersCommand && !offersRole && hasBase && hasAll
    console.log(
      `${s.name}: items=${items.length} offersCommand=${offersCommand} ` +
      `offersRole=${offersRole} ${ok ? 'OK' : 'MISMATCH'}`,
    )
  } else {
    const hasTrust = await page.getByRole('button', { name: /Trust/ }).count()
    ok = hasTrust === 1
    console.log(`${s.name}: trustButton=${hasTrust} ${ok ? 'OK' : 'MISMATCH'}`)
  }
  if (!ok) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
