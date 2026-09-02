/**
 * Real-browser proof for the Radix Select-inside-Radix-Dialog interaction that
 * the happy-dom unit tests cannot exercise faithfully.
 *
 * Radix fires its discrete events through
 * `ReactDOM.flushSync(() => target.dispatchEvent(event))`
 * (@radix-ui/react-primitive). Under Testing Library's `fireEvent` that dispatch
 * happens synchronously INSIDE React's batch, so the flushSync lands during a
 * render and React throws "Should not already be working." A real browser
 * dispatches the same click as a discrete task OUTSIDE any render pass.
 *
 * So this script drives the real thing and fails loudly on any console error:
 *   open the crew editor -> open the Memory Store select -> pick the value another
 *   crew already uses -> assert the collision warning appears.
 *
 * Usage: node scripts/verify-crews-dialog-select.mjs
 */
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'core-ws', memory_store: 'core-mem' },
  { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall-mem' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })

  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`))

  await stubDashboardApi(page, {
    extra: crewsApi({
      crews: CREWS,
      defaultAgent: 'kirocrew',
      memoryStores: ['core-mem', 'oncall-mem'],
    }),
  })

  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  await page.locator('[data-testid="crew-card"]').first().waitFor({ timeout: 15000 })

  // Open the editor on the NON-default crew (it has a danger zone and its own stores).
  await page.getByRole('button', { name: 'Edit crew oncall' }).click()
  const dialog = page.getByRole('dialog', { name: 'Edit crew oncall' })
  await dialog.waitFor({ timeout: 15000 })

  // No collision yet — oncall is on its own store.
  if (await dialog.getByText(/Also used by/).count()) {
    throw new Error('collision warning shown before any collision was created')
  }

  // THE interaction under test: a Radix Select opened and committed from inside
  // a Radix Dialog.
  await dialog.getByRole('combobox', { name: 'Memory Store' }).click()
  await page.getByRole('option', { name: 'core-mem' }).click()

  // Picking the store kirocrew already uses must surface the warning, by name.
  await dialog.getByText(/Also used by kirocrew/).waitFor({ timeout: 10000 })

  // The select must have actually committed the new value, not just warned.
  const committed = await dialog.getByRole('combobox', { name: 'Memory Store' }).textContent()
  if (!committed?.includes('core-mem')) {
    throw new Error(`select did not commit: trigger reads "${committed}"`)
  }

  // Nested dialog: the workspace select's action opens a second Radix layer, and
  // Escape must close ONLY that layer.
  await dialog.getByRole('combobox', { name: 'Workspace' }).click()
  await page.getByText('+ New workspace…').click()
  const nested = page.getByRole('dialog', { name: 'Create Workspace' })
  await nested.waitFor({ timeout: 10000 })
  await page.keyboard.press('Escape')
  await nested.waitFor({ state: 'detached', timeout: 10000 })
  if (!(await dialog.count())) {
    throw new Error('Escape on the nested dialog also closed the editor underneath')
  }

  // And Escape again closes the editor itself.
  await page.keyboard.press('Escape')
  await dialog.waitFor({ state: 'detached', timeout: 10000 })

  await browser.close()
  srv.close()

  const real = errors.filter(e => !/favicon|Failed to load resource/i.test(e))
  if (real.length) {
    console.error('CONSOLE ERRORS:\n' + real.join('\n'))
    process.exit(1)
  }
  console.log('OK: select-in-dialog commits, collision warning fires, nested Escape scoped, 0 console errors')
}

main().catch(err => { console.error(err); process.exit(1) })
