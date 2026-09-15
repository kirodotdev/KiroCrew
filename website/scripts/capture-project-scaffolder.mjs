/**
 * Screenshots of the Create Folders From Project app, one per introduced state.
 *
 * Drives the isolated capture entry (website/capture/project-scaffolder.html),
 * which mounts the REAL ProjectScaffolderPage with fetch stubbed to answer from
 * a synthetic monorepo, then walks the page the way a user does: type the root,
 * Scan, open the disclosure, Create. Each scene asserts the state under test is
 * on screen before writing the file, so a run never emits a misleading frame.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-project-scaffolder.mjs http://127.0.0.1:6824 ../temp-screenshots/create-folders-from-project
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/create-folders-from-project'
mkdirSync(OUT, { recursive: true })

const ROOT = '/work/acme-shop'

/** Type the root and press Scan. */
async function scan(page) {
  await page.getByLabel('Project directory').fill(ROOT)
  await page.getByRole('button', { name: 'Scan' }).click()
}

const SCENES = [
  {
    name: 'scan-preview',
    query: 'scan=ok',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      // Both tiers are on screen with their shipped labels, the disclosure is collapsed.
      const tiers = (await page.getByText('Confident').count()) > 0 && (await page.getByText('Possible match').count()) > 0
      return tiers && (await page.getByTestId('nested-list').count()) === 0
    },
  },
  {
    name: 'nested-open',
    query: 'scan=ok',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      await page.getByTestId('nested-toggle').click()
      await page.getByTestId('nested-list').waitFor()
      return (await page.getByText('Possible match').count()) > 0
    },
  },
  {
    name: 'results',
    query: 'scan=ok&create=ok',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      await page.getByRole('button', { name: 'Create sidebar folders' }).click()
      await page.getByTestId('scaffold-results').waitFor()
      // The refused row renders through the shared error surface, not a red div.
      return (await page.getByTestId('failed-rows').getByRole('alert').count()) === 1 && (await page.getByTestId('skipped-rows').count()) === 1
    },
  },
  {
    name: 'empty',
    query: 'scan=empty',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('scan-empty').waitFor()
      return true
    },
  },
  {
    name: 'stale',
    query: 'scan=ok&create=stale',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      await page.getByRole('button', { name: 'Create sidebar folders' }).click()
      await page.getByTestId('stale-selection').waitFor()
      // Create is disabled while the preview is stale; Re-scan is the way back.
      if (!(await page.getByRole('button', { name: 'Create sidebar folders' }).isDisabled())) return false
      return true
    },
  },
  {
    name: 'create-refused',
    query: 'scan=ok&create=refused',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      await page.getByRole('button', { name: 'Create sidebar folders' }).click()
      await page.getByTestId('create-error').waitFor()
      // The whole-call refusal renders through ErrorNotice beside the Create
      // button, scoped "No folders were created"; a rate limit is retryable,
      // so Create stays enabled (a moved tree routes to the stale banner instead).
      const notice = page.getByTestId('create-error')
      const scoped = ((await notice.textContent()) ?? '').includes('No folders were created')
      const enabled = await page.getByRole('button', { name: 'Create sidebar folders' }).isEnabled()
      return (await notice.getAttribute('role')) === 'alert' && scoped && enabled
    },
  },
  {
    name: 'creating',
    query: 'scan=ok&create=slow',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      await page.getByRole('button', { name: 'Create sidebar folders' }).click()
      // The in-flight create: the button reads "Creating" and is disabled.
      const btn = page.getByRole('button', { name: 'Creating' })
      await btn.waitFor()
      return await btn.isDisabled()
    },
  },
  {
    name: 'root-refused',
    query: 'scan=refused',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('root-error').waitFor()
      return (await page.getByTestId('root-error').getAttribute('role')) === 'alert'
    },
  },
  {
    name: 'root-drifted',
    query: 'scan=ok',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      // Type a new path without scanning it: Create must refuse until it is scanned.
      await page.getByLabel('Project directory').fill(`${ROOT}-other`)
      await page.getByTestId('root-drifted').waitFor()
      return await page.getByRole('button', { name: 'Create sidebar folders' }).isDisabled()
    },
  },
  {
    name: 'narrow-320',
    query: 'scan=ok',
    viewport: { width: 320, height: 1100 },
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      // The narrow layout: the path field spans the row and the two actions sit
      // beneath it, so the field is never squeezed by long translated labels.
      const field = await page.getByLabel('Project directory').boundingBox()
      const scanBtn = await page.getByRole('button', { name: 'Scan' }).boundingBox()
      return field !== null && scanBtn !== null && field.width > 240 && scanBtn.y > field.y + field.height - 1
    },
  },
  {
    name: 'picker-open',
    query: 'scan=ok',
    viewport: { width: 390, height: 700 },
    fullPage: true,
    drive: async (page) => {
      // The shared ProjectPicker clamps to the viewport (max-w-[calc(100vw-16px)]):
      // open it on a phone-width frame and check the panel is not wider than the screen.
      await page.getByTestId('scaffolder-browse').click()
      await page.getByRole('option').first().waitFor()
      // The panel is the fixed 400px box; the clamp must keep it inside a 390px viewport.
      const panel = await page.locator('div.fixed.w-\\[400px\\]').first().boundingBox()
      return panel !== null && panel.x >= 0 && panel.x + panel.width <= 390
    },
  },
  {
    name: 'root-new',
    query: 'scan=ok-newroot',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      // The root has no folder yet, so the counter promises it.
      return ((await page.getByTestId('selected-count').textContent()) ?? '').startsWith('Root folder +')
    },
  },
  {
    name: 'scanning',
    query: 'scan=ok-then-slow',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      // Re-scan into a request that never answers: the field says Scanning and
      // the kept preview is a disabled, dimmed fieldset until it resolves.
      await page.getByRole('button', { name: 'Scan' }).click()
      await page.getByRole('button', { name: 'Scanning' }).waitFor()
      // Playwright's isDisabled() does not cover <fieldset>; read the attribute.
      return (await page.getByTestId('preview-card').getAttribute('disabled')) !== null
    },
  },
  {
    name: 'rescan-failed',
    query: 'scan=ok-then-fail',
    drive: async (page) => {
      await scan(page)
      await page.getByTestId('preview-group').waitFor()
      // Hand-tune the selection, then re-scan into a failure.
      await page.getByTestId('preview-group').getByRole('button', { name: 'Select all' }).click()
      await page.getByRole('button', { name: 'Scan' }).click()
      await page.getByTestId('root-error').waitFor()
      // The preview survived the failed re-scan and is usable again.
      const previewStillThere = (await page.getByTestId('preview-group').count()) === 1
      const enabled = (await page.getByTestId('preview-card').getAttribute('disabled')) === null
      const kept = (await page.getByTestId('preview-kept').count()) === 1
      // The kept preview is usable (tickable) but not confirmable until a scan succeeds.
      const createDisabled = await page.getByRole('button', { name: 'Create sidebar folders' }).isDisabled()
      return previewStillThere && enabled && kept && createDisabled
    },
  },
]

const browser = await chromium.launch()
let failed = false
for (const s of SCENES) {
  const page = await browser.newPage({ viewport: s.viewport ?? { width: 960, height: 900 }, deviceScaleFactor: 2 })
  await page.goto(`${BASE}/capture/project-scaffolder.html?${s.query}&theme=dark`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  let ok = false
  try {
    ok = await s.drive(page)
  } catch (err) {
    console.error(`${s.name}: ${err instanceof Error ? err.message : String(err)}`)
  }
  console.log(`${s.name}: ${ok ? 'state under test on screen' : 'STATE MISSING'}`)
  if (!ok) { failed = true; await page.close(); continue }
  if (s.fullPage) {
    // A portal (the picker) renders outside the capture root, so frame the viewport.
    await page.screenshot({ path: `${OUT}/${s.name}.png` })
  } else {
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
  }
  await page.close()
}

await browser.close()
if (failed) {
  console.error('a scene never reached its state — no misleading frame written for it')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
