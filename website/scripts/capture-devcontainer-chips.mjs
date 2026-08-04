/**
 * Capture the composer's Dev Container chip row.
 *
 * The trust-card harness beside this one shoots the consent card; this one exists
 * for the chips, which live in the composer and are the surface the two-action
 * cap applies to. Trust is HELD and a container is RUNNING here, which is the
 * state in which both chips mount -- the trust chip (project-scoped, owns the exit
 * from trust) and the execution chip (where this session actually ran).
 *
 * Same gateway-free harness as the sibling script: serveDist + stubDashboardApi,
 * no live backend.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/devcontainer-trust'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const PROJECT = '/home/dev/work/payments-api'
const CONFIG_PATH = `${PROJECT}/.devcontainer/devcontainer.json`

// Trusted AND running: the only state where both chips are mounted at once, which
// is what the placement question is about.
const STATUS = {
  project_dir: PROJECT,
  enabled: true,
  has_config: true,
  config_path: CONFIG_PATH,
  trusted: true,
  container_id: 'a1b2c3d4e5f67890',
  running: true,
  remote_workspace_folder: '/workspaces/payments-api',
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    slots: [{
      key: 's1', title: 'payments-api', messages: 2, running: false,
      agent: 'kirocrew', mode: '', created: '2026-08-01T01:00:00Z',
      last_ts: '2026-08-11T12:00:00Z', folder_id: '', project: PROJECT,
      execution: { mode: 'container', container_name: 'payments-api_devcontainer', reason: null },
    }],
    extra: async (path, route) => {
      if (path.startsWith('/api/devcontainer/status')) {
        await json(route, STATUS)
        return true
      }
      return false
    },
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

  // Anchor on the chip's own accessible name rather than a class: the chip is a
  // menu button and that name is asserted by its unit tests too.
  const chip = page.getByRole('button', { name: /dev container/i }).first()
  await chip.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)

  const shots = []
  const save = async (name, locator, clip) => {
    const path = `${OUT}/${PREFIX}-${name}.png`
    await (locator || page).screenshot(clip ? { path, clip } : { path })
    shots.push(path)
  }

  // The composer region, so the chip row is visible IN CONTEXT relative to the
  // shelf above it -- a crop of the chip alone could not show which row it is in,
  // which is the entire point of this capture.
  // Clip around the chip rather than matching a container selector: the crop has
  // to include the shelf row ABOVE the chip, since which row the chip sits in is
  // the entire point of this capture and a crop of the chip alone cannot show it.
  const box = await chip.boundingBox()
  if (!box) throw new Error('chip has no bounding box')
  const clip = {
    x: Math.max(0, box.x - 40),
    y: Math.max(0, box.y - 150),
    width: 900,
    height: box.height + 200,
  }
  await save('chip-row', null, clip)

  // Open the menu: the chip's only action is withdrawing trust, and a menu
  // trigger counts as one control however many items it holds.
  await chip.click()
  await page.waitForTimeout(300)
  await save('chip-menu')

  await browser.close()
  srv.close()
  for (const s of shots) console.log(s)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
