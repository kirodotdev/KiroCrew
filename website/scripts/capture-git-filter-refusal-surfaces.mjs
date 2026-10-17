/**
 * Real-browser evidence for the filter-driver REFUSAL on the two surfaces
 * outside the Git panel.
 *
 * Drives the isolated capture entry (website/capture/git-filter-refusal-surfaces.html),
 * which mounts the REAL `FileBrowserRail` and the REAL `PierreWorkspaceTreeImpl`
 * with `/api/project/git/status` answering the handler's own 503 body.
 *
 * The unit suites pin that both surfaces recognise the refusal code
 * (src/test/PierreWorkspaceTreeImpl.test.tsx). This exists for what a DOM
 * assertion cannot carry: whether a two-sentence refusal with a title READS at
 * rail and tree width, which is what a reviewer asked to see before believing
 * the copy fits there.
 *
 * Frames:
 *   01-rail-declared     the chat side rail, driver-declared cause
 *   02-tree-declared     the Pierre Changed tree, same cause
 *   03-rail-unreadable   the other cause, which promises nothing about permanence
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell (website/)
 *   node scripts/capture-git-filter-refusal-surfaces.mjs http://127.0.0.1:6841 /tmp/shots
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/git-filter-refusal-surfaces'
mkdirSync(OUT, { recursive: true })

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

/** [file suffix, surface, cause, viewport]. */
const FRAMES = [
  ['git-refusal-1-rail-declared', 'rail', 'declared', { width: 460, height: 700 }],
  ['git-refusal-2-tree-declared', 'tree', 'declared', { width: 360, height: 700 }],
  ['git-refusal-3-rail-unreadable', 'rail', 'unreadable', { width: 460, height: 700 }],
]

for (const [name, surface, cause, viewport] of FRAMES) {
  const page = await browser.newPage({ viewport })
  page.on('pageerror', e => { console.error(`[${name}] pageerror:`, e.message); failures++ })
  await page.goto(
    `${BASE}/capture/git-filter-refusal-surfaces.html?theme=dark&surface=${surface}&cause=${cause}`,
    { waitUntil: 'networkidle' },
  )

  // Wait on the NOTICE, not on a timer: a frame taken before it renders would
  // photograph an empty surface and still be reported as evidence.
  const notice = surface === 'tree'
    ? page.locator('[data-testid="workspace-tree-status-error"]')
    : page.locator('[role="alert"]').first()
  await notice.waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)

  // Print what the notice SAYS, so a frame cannot silently disagree with the
  // code, and count the notices so a duplicate render is visible as a number.
  console.log(name, JSON.stringify({
    text: (await notice.innerText()).trim(),
    titles: await notice.locator('strong').count(),
    notices: await page.locator('[role="alert"]').count(),
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
