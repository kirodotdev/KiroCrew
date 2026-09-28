/**
 * Screenshot harness for the Command Bar's COPY states.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli — which is what lets it run on a host where the
 * pod's port-ownership proof cannot be made.
 *
 * The five frames are the states a reader actually meets, in order:
 *   1. the chord NAMED in the footer, while the selected row has an address
 *   2. a dashboard link copied — the derived case, which is most rows
 *   3. a DEPLOYED artifact copied — the one row whose address is not this dashboard
 *   4. a clipboard write that did not land, through the product's error surface
 *   5. a row with no address, answered rather than left silent
 *
 * Frames 2 and 3 are the pair worth photographing most: they look nearly identical
 * and carry different addresses, which is exactly why the notice shows the address
 * rather than a bare "Copied".
 *
 * Usage: node scripts/capture-command-bar-copy.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/command-bar-copy'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'

/**
 * The launcher itself, as a builtin so it claims the quick-search slot. Nothing else
 * is needed: the rows these frames copy are the launcher's own (a settings row) and
 * an artifact from the scoped view below.
 */
const APPS = [
  {
    name: 'command-bar',
    displayName: 'Command Bar',
    enabled: true,
    origin: 'builtin',
    source: 'builtin',
    version: '0.1.0',
    manifest: {
      name: 'command-bar',
      displayName: 'Command Bar',
      version: '0.1.0',
      ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
    },
  },
]

/**
 * One artifact, DEPLOYED.
 *
 * `webapp_metadata.deploy_target.public_url` is the whole point of frame 3: this row
 * opens a page on this dashboard, and the address worth handing to another person is
 * the one that does not need this dashboard at all.
 */
const ARTIFACTS = [
  {
    slug: 'kanban-board',
    name: 'Kanban Board',
    kind: 'webapp',
    description: 'Sprint board, deployed',
    tags: [],
    version: 2,
    updated_at: '2026-09-21T09:00:00Z',
    webapp_metadata: {
      deploy_target: { public_url: 'https://d2nzmpzyp0popu.cloudfront.net/kanban-board/' },
    },
  },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openBar({ denyClipboard = false } = {}) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()

  if (denyClipboard) {
    // Both layers refused, because the helper has two: the async Clipboard API, and a
    // `document.execCommand('copy')` fallback for the contexts where the first does not
    // exist. Breaking only one would photograph a success. This is not a contrived
    // state — it is what a plain-HTTP LAN gateway and a sandboxed null-origin document
    // actually do.
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: () => Promise.reject(new Error('clipboard blocked')) },
      })
      document.execCommand = () => false
    })
  }

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, APPS)
      return true
    }
    if (path.startsWith('/api/artifacts')) {
      await json(route, { artifacts: ARTIFACTS })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'default', mode: '' }],
    extra,
  })

  await page.goto(`${base}/chat`)
  await page.waitForLoadState('networkidle')
  // The quick-search chord. The overlay claims the slot, so this opens the launcher.
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role="dialog"]', { timeout: 10_000 })
  return { context, page }
}

async function shot(page, name) {
  await page.waitForTimeout(350)
  const file = join(OUT, name)
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
}

/**
 * Walk the keyboard onto a row that HAS an address, and prove it by the footer.
 *
 * Not "type and assume the top hit": the best match for a settings word is "Toggle
 * Theme", an `invoke` row that correctly has no address, so a frame shot there would
 * be photographing frame 5 by accident.
 */
async function selectAddressableRow(page) {
  await page.getByRole('combobox').fill('theme')
  const hint = page.getByText('Copy link')
  for (let step = 0; step < 12; step += 1) {
    if (await hint.isVisible().catch(() => false)) return
    await page.getByRole('combobox').press('ArrowDown')
    await page.waitForTimeout(60)
  }
  throw new Error('no row with an address within 12 steps')
}

// ── 1. the chord, named while the selected row has an address ───────────────
{
  const { context, page } = await openBar()
  await selectAddressableRow(page)
  await shot(page, '1-copy-hint.png')
  await context.close()
}

// ── 2. a dashboard link copied ─────────────────────────────────────────────
{
  const { context, page } = await openBar()
  await selectAddressableRow(page)
  await page.getByRole('combobox').press('Control+c')
  // The ADDRESS, not just the word: the notice is only useful if it says which one.
  await page.getByRole('status').filter({ hasText: '/settings/' }).waitFor({ timeout: 5_000 })
  await shot(page, '2-copied-dashboard-link.png')
  await context.close()
}

// ── 3. a deployed artifact's public URL copied ──────────────────────────────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('artifact')
  await page
    .getByRole('option')
    .filter({ hasText: 'Search Artifacts' })
    .first()
    .dispatchEvent('mousedown')
  await page.getByRole('option').filter({ hasText: 'Kanban Board' }).first().waitFor({ timeout: 5_000 })
  await page.getByRole('combobox').press('Control+c')
  await page.getByRole('status').filter({ hasText: 'cloudfront.net' }).waitFor({ timeout: 5_000 })
  await shot(page, '3-copied-deployed-artifact.png')
  await context.close()
}

// ── 4. a write that did not land ───────────────────────────────────────────
{
  const { context, page } = await openBar({ denyClipboard: true })
  await selectAddressableRow(page)
  await page.getByRole('combobox').press('Control+c')
  // Through the product's error surface, which is what `role="alert"` proves here.
  // Scoped INSIDE the dialog: the SPA ships a hidden `#boot-failure` alert at the
  // document root, so an unscoped selector resolves to that one and waits forever.
  await page
    .locator('[role="dialog"] [role="alert"]')
    .filter({ hasText: 'Copy failed' })
    .waitFor({ timeout: 5_000 })
  await shot(page, '4-copy-failed.png')
  await context.close()
}

// ── 5. a row with no address ───────────────────────────────────────────────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('toggle theme')
  await page.getByRole('option').filter({ hasText: 'Toggle Theme' }).first().waitFor({ timeout: 5_000 })
  await page.getByRole('combobox').press('Control+c')
  // Scoped inside the dialog and matched on its text: the page carries other live
  // regions, so a bare role lookup is ambiguous here.
  await page
    .locator('[role="dialog"] [role="status"]')
    .filter({ hasText: 'Nothing here to copy.' })
    .waitFor({ timeout: 5_000 })
  await shot(page, '5-nothing-to-copy.png')
  await context.close()
}

await browser.close()
srv.close()
