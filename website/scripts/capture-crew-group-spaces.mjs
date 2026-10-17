/**
 * Screenshot harness, and behaviour check, for spaces inside a crew group: the
 * crew's own folders nest its chats, unfiled chats last.
 *
 *   before    -- rows without `folder_id` (`?flat=1`), what main sends: one flat list.
 *   after     -- the crew's folders, nested, with an empty folder left out.
 *   collapsed -- one press on a space closes it alone (pointer moved off the row).
 *   failed    -- the crew's folder read fails: the chats list flat under a notice.
 *
 * Usage: node scripts/capture-crew-group-spaces.mjs [devBase] [outDir]
 */
import { openSessionTreeHarness } from './lib/session-tree-harness.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/crew-group-spaces'

const { page, check, settleTheme, shot, finish } = await openSessionTreeHarness(OUT)
// Wide enough that the sidebar paints its stored 520px (a narrow window clamps it
// to the 180px floor), and tall enough for a local space, the crew group, its
// spaces and the unfiled chat.
await page.setViewportSize({ width: 1400, height: 960 })

async function load(query) {
  await page.goto(`${BASE}/capture/crew-group-spaces.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector('[data-testid="crew-group-astro"] [data-slot-key="chat-35"]')
  await settleTheme()
  await page.waitForTimeout(400)
}

await load('&flat=1')
check('before: no space in the crew group', !(await page.$('[data-testid^="crew-space-"]')))
await shot('before-flat-crew-group')

await load('')
await page.waitForSelector('[data-testid="crew-space-astro-f-docs"]')
await page.waitForTimeout(300)
check('after: API guides nests under Docs',
  !!(await page.$('[data-testid="crew-space-astro-f-docs"] [data-testid="crew-space-astro-f-api"]')))
check('after: Docs counts its own chat and its subfolder\'s',
  (await page.textContent('[data-testid="crew-space-toggle-astro-f-docs"]'))?.includes('3'))
check('after: the empty Archive is left out', !(await page.$('[data-testid="crew-space-astro-f-empty"]')))
check('after: the unfiled chat sits outside every space',
  !(await page.$('[data-testid^="crew-space-"] [data-slot-key="chat-35"]')))
await shot('after-crew-group-spaces')

await page.click('[data-testid="crew-space-toggle-astro-f-support"]')
// Off the row, so the shot shows the closed state and not a hover.
await page.mouse.move(5, 5)
await page.waitForTimeout(500)
const expanded = await page.getAttribute('[data-testid="crew-space-toggle-astro-f-support"]', 'aria-expanded')
check('collapsed: Customer Support closes on its own', expanded === 'false', `aria-expanded=${expanded}`)
await shot('collapsed-one-space')

await load('&folders=fail')
await page.waitForSelector('[data-testid="crew-group-spaces-error-astro"]')
await page.waitForTimeout(300)
check('failed: the notice sits in the crew group',
  !!(await page.$('[data-testid="crew-group-astro"] [data-testid="crew-group-spaces-error-astro"]')))
check('failed: the chats still list, with no space', !(await page.$('[data-testid^="crew-space-"]')))
await shot('folder-read-failed')

await finish()
