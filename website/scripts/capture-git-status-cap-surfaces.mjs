/**
 * Real-browser evidence for the CAPPED git-status listing on the two surfaces
 * outside the Git panel.
 *
 * Drives the isolated capture entry (website/capture/git-status-cap-surfaces.html),
 * which mounts the REAL `FileBrowserRail` and the REAL `PierreWorkspaceTreeImpl`
 * with `/api/project/git/status` answering 500 entries plus `truncated: true`.
 *
 * The unit suites pin that both surfaces read the flag
 * (src/test/FileBrowserRail.test.tsx, src/test/PierreWorkspaceTreeImpl.test.tsx).
 * This exists for what a DOM assertion cannot carry: whether a bare `+` on a
 * 10px tabular badge reads as "at least this many" at rail width, and whether one
 * muted notice row above a long list is noticed at all.
 *
 * Frames:
 *   01-rail-capped     the chat rail's Changed badge, listing capped
 *   02-rail-complete   the control: same count, listing complete, no marker
 *   03-tree-capped     the Pierre Changed tree with the notice above the list
 *   04-tree-complete   the control: same list, no notice
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell (website/)
 *   node scripts/capture-git-status-cap-surfaces.mjs http://127.0.0.1:6842 /tmp/shots
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/git-status-cap-surfaces'
mkdirSync(OUT, { recursive: true })

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const RAIL_BADGE = '[data-testid="file-browser-rail-changed-count"]'
const RAIL_CHANGED_BTN = 'button:has([data-testid="file-browser-rail-changed-count"])'
const TREE_NOTICE = '[data-testid="workspace-tree-changed-truncated"]'
const TREE_ROWS = 'file-tree-container'

/** [file suffix, surface, capped, viewport]. */
const FRAMES = [
  ['git-status-cap-1-rail-capped', 'rail', true, { width: 460, height: 700 }],
  ['git-status-cap-2-rail-complete', 'rail', false, { width: 460, height: 700 }],
  ['git-status-cap-3-tree-capped', 'tree', true, { width: 360, height: 700 }],
  ['git-status-cap-4-tree-complete', 'tree', false, { width: 360, height: 700 }],
]

for (const [name, surface, capped, viewport] of FRAMES) {
  const page = await browser.newPage({ viewport })
  page.on('pageerror', e => { console.error(`[${name}] pageerror:`, e.message); failures++ })
  await page.goto(
    `${BASE}/capture/git-status-cap-surfaces.html?theme=dark&surface=${surface}&capped=${capped}`,
    { waitUntil: 'networkidle' },
  )

  // Wait on the thing the frame is ABOUT, never on a timer: both markers only
  // render once the status query has resolved, so a frame taken earlier would
  // photograph a surface with no count at all and still be reported as evidence.
  // The complete-listing controls have no marker to wait for, so they wait on the
  // surface's own content instead -- the marker's ABSENCE is only evidence once
  // the state that would have produced it has arrived.
  if (surface === 'rail') {
    await page.locator(RAIL_BADGE).waitFor({ timeout: 20000 })
  } else {
    await page.locator(TREE_ROWS).waitFor({ timeout: 20000 })
    await page.waitForFunction(
      sel => (document.querySelector(sel)?.clientHeight ?? 0) > 100,
      TREE_ROWS,
      { timeout: 20000 },
    )
    if (capped) await page.locator(TREE_NOTICE).waitFor({ timeout: 20000 })
  }
  await page.waitForTimeout(400)

  // Print what each surface SAYS, so a frame cannot silently disagree with the
  // code, and count the marker so a duplicate render is visible as a number.
  const badge = page.locator(RAIL_BADGE)
  const notice = page.locator(TREE_NOTICE)
  console.log(name, JSON.stringify({
    badges: await badge.count(),
    badge: (await badge.count()) ? (await badge.first().innerText()).trim() : null,
    badgeTitle: (await badge.count())
      ? await page.locator(RAIL_CHANGED_BTN).getAttribute('title')
      : null,
    notices: await notice.count(),
    noticeText: (await notice.count()) ? (await notice.first().innerText()).trim() : null,
  }))

  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('SHOT', name)
  await page.close()
}

await browser.close()
if (failures > 0) {
  console.error(`FAILED: ${failures} page error(s)`)
  process.exit(1)
}
console.log('DONE', OUT)
