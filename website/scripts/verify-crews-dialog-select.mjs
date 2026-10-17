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
 *   open the default crew's editor -> open the Workspace select -> pick the
 *   workspace another crew already uses -> assert the collision warning appears.
 *
 * Usage: npm run build && npm run verify:crews-dialog-select
 *
 * On demand only: no CI lane runs this script (a maintainer CI-policy decision,
 * #10625). It guards nothing until someone runs it.
 */
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

// The collision warning is only shown in the default assistant's editor
// (CrewPlacePane), so the crew under edit is `default`, on the Global V1 store.
const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'core-ws', memory_store: 'default' },
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
      defaultAgent: 'default',
      memoryStores: ['default', 'oncall-mem'],
    }),
  })

  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  await page.locator('[data-testid="crew-card"]').first().waitFor({ timeout: 15000 })

  await page.getByRole('button', { name: 'Edit crewmate default' }).click()
  const dialog = page.getByRole('dialog', { name: 'Edit crewmate default' })
  await dialog.waitFor({ timeout: 15000 })

  // The binding pickers live on the editor's "Workspace · Memory" pane.
  await dialog.getByTestId('crew-rail-place').click()

  // No collision yet — default is on its own workspace.
  if (await dialog.getByText(/Also used by/).count()) {
    throw new Error('collision warning shown before any collision was created')
  }

  // THE interaction under test: a Radix Select opened and committed from inside
  // a Radix Dialog. The workspace is the editable binding (an existing crew's
  // memory store is read-only in the editor).
  await dialog.getByRole('combobox', { name: 'Workspace' }).click()
  await page.getByRole('option', { name: 'oncall' }).click()

  // Picking the workspace oncall already uses must surface the warning, by name.
  await dialog.getByText(/Also used by oncall/).waitFor({ timeout: 10000 })

  // The select must have actually committed the new value, not just warned.
  const committed = await dialog.getByRole('combobox', { name: 'Workspace' }).textContent()
  if (!committed?.includes('oncall')) {
    throw new Error(`select did not commit: trigger reads "${committed}"`)
  }

  // Nested dialog: the workspace select's action opens a second Radix layer, and
  // Escape must close ONLY that layer.
  await dialog.getByRole('combobox', { name: 'Workspace' }).click()
  await page.getByText('+ New workspace…').click()
  const nested = page.getByRole('dialog', { name: 'Create Workspace' })
  await nested.waitFor({ timeout: 10000 })
  // The closing select's menu is its own dismissable layer. Until it unmounts
  // it can still be the top layer, and an Escape sent then closes it instead
  // of the nested dialog, so the step failed at random.
  await page.getByRole('listbox').waitFor({ state: 'detached', timeout: 10000 })
  await page.keyboard.press('Escape')
  await nested.waitFor({ state: 'detached', timeout: 10000 })
  if (!(await dialog.count())) {
    throw new Error('Escape on the nested dialog also closed the editor underneath')
  }

  // Escape again asks to close the editor itself. The workspace pick above is
  // unsaved, so the editor first asks whether to discard it.
  await page.keyboard.press('Escape')
  const discard = page.getByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })
  await discard.waitFor({ timeout: 10000 })
  await discard.getByTestId('crew-sched-discard-confirm').click()
  // A CSS locator, not getByRole: while another layer is open Radix marks the
  // editor aria-hidden, and getByRole would then read it as already gone.
  await page.locator('[role="dialog"][aria-label="Edit crewmate default"]')
    .waitFor({ state: 'detached', timeout: 10000 })

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
