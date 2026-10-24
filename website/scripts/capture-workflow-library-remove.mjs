/**
 * Screenshots for removing a saved workflow from the Workflow library (#18998).
 *
 * Drives the ISOLATED capture entry (website/capture/workflow-library-remove.html),
 * which mounts the REAL WorkflowLibraryTab with the definition routes answered
 * by a fixture.
 *
 * Frames, each in dark and light:
 *  - action:   the selected workflow with Remove on the far side of the button row, apart from Save new revision.
 *  - confirm:  the confirmation, reached by clicking the real Remove button.
 *  - removed:  after the 200, the row is gone, a note names the removed workflow,
 *              and the next workflow is selected.
 *  - removed-last: after the 200 on the only saved workflow, the note names it above the
 *              empty library, and nothing is selected.
 *  - failed:   after a 500, the row stays and a Try removing again button follows the notice. The cause
 *              sits behind a collapsed Details disclosure.
 *  - failed-details: the same notice with Details opened, showing the status, the backend code and the
 *              failing path.
 *  - pending:  while the DELETE is in flight, the button reads Removing… and is disabled.
 *  - pending-other: the same DELETE still in flight after the user picks another workflow: that
 *              workflow's Remove keeps its own label and is only disabled, with no busy state.
 *  - retry-pending: after a 500, the retry is in flight. The failure line is hidden, and the focused
 *              retry reads Removing… with aria-busy and aria-disabled. The editor's Remove keeps its own
 *              label and is only disabled, so the retry is the one progress cue.
 *  - reserved: a removed workflow holds Debug Project's slug, so its row and the editor
 *              header read /workflow wfd_debug, no /workflow debug-project is offered, and a
 *              hint under the Slash-command name field says why.
 *  - confirm-reserved: that workflow's confirmation names /workflow wfd_debug, the command
 *              the removal stops, and /workflow debug-project, which was already reserved.
 *  - reserved-both: removed workflows hold both its id and its slug, so the row and the
 *              header read "No slash command", a note says what to do, and
 *              Save new revision, Run workflow and Remove from library are disabled.
 *
 * Each frame ASSERTS the text that makes it that state.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort   # in another shell
 *   node scripts/capture-workflow-library-remove.mjs http://127.0.0.1:6814 ../temp-screenshots/18998-workflow-library-remove
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/18998-workflow-library-remove'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1100, height: 1080 }
const REMOVE = 'Remove from library'

async function open(page, scene, theme) {
  await page.goto(`${BASE}/capture/workflow-library-remove.html?scene=${scene}&theme=${theme}`)
  await page.getByRole('button', { name: REMOVE }).waitFor()
}

async function confirmRemove(page) {
  await page.getByRole('button', { name: REMOVE }).click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  // The Modal enters with a short fade; wait for it to finish rather than sleep.
  await page.waitForFunction(() => {
    const el = document.querySelector('[role="dialog"]')
    return el && getComputedStyle(el).opacity === '1'
  })
  return dialog
}

// Btn fades between its enabled and disabled looks, and a list row fades its
// selected background, so a shot waits two frames for a style change to start
// its transitions, then for every CSS transition to finish. Spinners are CSS
// animations, not transitions, and keep running.
async function shoot(page, path) {
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  )
  await page.waitForFunction(() =>
    document
      .getAnimations()
      .every((animation) => !(animation instanceof CSSTransition) || animation.playState !== 'running'),
  )
  await page.screenshot({ path })
}

const browser = await chromium.launch({
  args: ['--host-resolver-rules=MAP localhost 127.0.0.1'],
})
try {
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: VIEWPORT })

    await open(page, 'ok', theme)
    await page.getByText('/workflow debug-project').first().waitFor()
    await shoot(page, `${OUT}/action-${theme}.png`)

    const dialog = await confirmRemove(page)
    const text = await dialog.innerText()
    if (!text.includes('Remove workflow \u201cDebug Project\u201d?')) throw new Error(`confirm title missing: ${text}`)
    if ((await dialog.getByRole('listitem').count()) !== 3) throw new Error('confirm effects are not a 3-item list')
    if (!text.includes('Past runs and workflows adapted from it keep working.')) {
      throw new Error(`confirm body missing: ${text}`)
    }
    if (!text.includes("Kiro Crew can't undo this.")) {
      throw new Error(`confirm permanence missing: ${text}`)
    }
    if (
      !text.includes(
        "/workflow debug-project stops working and stays reserved. A new workflow can't take that name and gets a numbered one instead.",
      )
    ) {
      throw new Error(`confirm slug bullet missing: ${text}`)
    }
    await shoot(page, `${OUT}/confirm-${theme}.png`)

    await dialog.getByRole('button', { name: REMOVE }).click()
    await page.waitForFunction(
      () => !document.body.innerText.includes('/workflow debug-project'),
    )
    await page.getByText('/workflow triage-tickets').first().waitFor()
    const note = await page.getByRole('status').innerText()
    if (!note.includes('Removed \u201cDebug Project\u201d from the library.')) throw new Error(`removed note missing: ${note}`)
    // The dialog plays an exit fade; shoot once it has left the DOM.
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    await shoot(page, `${OUT}/removed-${theme}.png`)

    await open(page, 'last', theme)
    const lastDialog = await confirmRemove(page)
    await lastDialog.getByRole('button', { name: REMOVE }).click()
    await page.waitForFunction(
      () => !document.body.innerText.includes('/workflow debug-project'),
    )
    const lastNote = await page.getByRole('status').innerText()
    if (!lastNote.includes('Removed \u201cDebug Project\u201d from the library.')) {
      throw new Error(`removed note missing over the empty library: ${lastNote}`)
    }
    await page.getByText('No saved workflows found.').waitFor()
    await page.getByText('Select a saved workflow or create a new one.').waitFor()
    // The dialog's own confirm is also named Remove, so count once it has left the DOM.
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    if ((await page.getByRole('button', { name: REMOVE }).count()) !== 0) {
      throw new Error('a Remove button is left once the library is empty')
    }
    await shoot(page, `${OUT}/removed-last-${theme}.png`)

    await open(page, 'fails', theme)
    const failing = await confirmRemove(page)
    await failing.getByRole('button', { name: REMOVE }).click()
    await page.getByRole('alert').waitFor()
    const alert = await page.getByRole('alert').innerText()
    if (!alert.includes("Couldn't remove \u201cDebug Project\u201d. It's still in the library.")) throw new Error(`error line missing: ${alert}`)
    // The retry button below says what to do, so the line does not repeat it.
    if (alert.includes('Try again')) throw new Error(`error line repeats the retry: ${alert}`)
    if (alert.includes('workflow_definition_write_failed')) {
      throw new Error(`cause is visible before Details is opened: ${alert}`)
    }
    // The retry follows the notice, described by it, so the user acts where the failure is reported.
    const failedRetry = page.getByRole('button', { name: 'Try removing again' })
    await failedRetry.waitFor()
    if (await failedRetry.evaluate((el) => el.closest('[role="alert"]') !== null)) {
      throw new Error('the retry sits inside the failure notice')
    }
    const alertId = await page.getByRole('alert').getAttribute('id')
    if (!alertId || (await failedRetry.getAttribute('aria-describedby')) !== alertId) {
      throw new Error('the retry is not described by the failure notice')
    }
    const details = page.getByRole('alert').getByRole('button', { name: 'Details' })
    if ((await details.getAttribute('aria-expanded')) !== 'false') throw new Error('Details is not collapsed')
    await page.getByText('/workflow debug-project').first().waitFor()
    // The dialog plays an exit fade; shoot once it has left the DOM.
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    await shoot(page, `${OUT}/failed-${theme}.png`)

    await details.focus()
    await page.keyboard.press('Enter')
    const opened = await page.getByRole('alert').innerText()
    if (!opened.includes('500 \u00b7 workflow_definition_write_failed \u00b7 /api/workflows/definitions/wfd_debug')) {
      throw new Error(`error cause line missing: ${opened}`)
    }
    await shoot(page, `${OUT}/failed-details-${theme}.png`)

    await open(page, 'pending', theme)
    const waiting = await confirmRemove(page)
    await waiting.getByRole('button', { name: REMOVE }).click()
    const pendingButton = page.getByRole('button', { name: 'Removing\u2026' })
    await pendingButton.waitFor()
    if (!(await pendingButton.isDisabled())) throw new Error('pending button is not disabled')
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    await shoot(page, `${OUT}/pending-${theme}.png`)

    // The removal still in flight is Debug Project's: Triage Tickets' Remove is not its cue.
    await page.getByText('Triage Tickets').first().click()
    await page.getByText('/workflow triage-tickets').nth(1).waitFor()
    const otherRemove = page.getByRole('button', { name: REMOVE })
    await otherRemove.waitFor()
    if (!(await otherRemove.isDisabled())) throw new Error('another workflow\u2019s Remove is not disabled')
    if ((await otherRemove.getAttribute('aria-busy')) !== null) {
      throw new Error('another workflow\u2019s Remove is busy')
    }
    if ((await page.getByText('Removing\u2026').count()) !== 0) {
      throw new Error('a Removing\u2026 cue shows on another workflow')
    }
    // The pointer leaves the row it picked, so the row shows as selected, not hovered.
    await page.mouse.move(1, 1)
    await shoot(page, `${OUT}/pending-other-${theme}.png`)

    await open(page, 'retry-pending', theme)
    const retried = await confirmRemove(page)
    await retried.getByRole('button', { name: REMOVE }).click()
    await page.getByRole('dialog').waitFor({ state: 'detached' })
    const retry = page.getByRole('button', { name: 'Try removing again' })
    await retry.focus()
    await page.keyboard.press('Enter')
    const retryPending = page.getByRole('button', { name: 'Removing\u2026' })
    await retryPending.waitFor()
    if ((await retryPending.getAttribute('aria-busy')) !== 'true') throw new Error('retry is not aria-busy')
    if ((await retryPending.getAttribute('aria-disabled')) !== 'true') throw new Error('retry is not aria-disabled')
    if (!(await retryPending.evaluate((el) => el === document.activeElement))) {
      throw new Error('focus left the retry while it is pending')
    }
    // The failure the retry answers is not the outcome while it runs.
    if ((await page.getByRole('alert').count()) !== 0) {
      throw new Error('the failure line shows while the retry is pending')
    }
    // The retry is the one progress cue: the editor's Remove keeps its label.
    if ((await page.getByText('Removing\u2026').count()) !== 1) {
      throw new Error('more than one Removing\u2026 cue while the retry is pending')
    }
    if (!(await page.getByRole('button', { name: REMOVE }).isDisabled())) {
      throw new Error('editor Remove is not disabled while the retry is pending')
    }
    await shoot(page, `${OUT}/retry-pending-${theme}.png`)

    await open(page, 'reserved', theme)
    // The row and the editor header both offer the id; the slug a removed workflow holds is offered nowhere.
    const offered = page.getByText('/workflow wfd_debug', { exact: true })
    await offered.nth(1).waitFor()
    if ((await offered.count()) !== 2) {
      throw new Error('the reserved-slug workflow is not offered by its id in the row and the header')
    }
    if (await page.getByText('/workflow debug-project', { exact: true }).count()) {
      throw new Error('a slug a removed workflow holds is still offered')
    }
    await page.getByText('/workflow triage-tickets').first().waitFor()
    // The field still holds the slug a removed workflow holds, so a hint says why it runs by id.
    const hint = page.getByText('A removed workflow holds this name, so this workflow runs as /workflow wfd_debug.', { exact: false })
    await hint.waitFor()
    await shoot(page, `${OUT}/reserved-${theme}.png`)

    // The confirmation names the id the removal goes to and the slug that was already held.
    const reservedDialog = await confirmRemove(page)
    const reservedText = await reservedDialog.innerText()
    if (
      !reservedText.includes(
        '/workflow wfd_debug stops working and stays reserved. /workflow debug-project was already reserved by a removed workflow.',
      )
    ) {
      throw new Error(`confirm reserved-slug line missing: ${reservedText}`)
    }
    await shoot(page, `${OUT}/confirm-reserved-${theme}.png`)

    await open(page, 'reserved-both', theme)
    // No ref reaches it: the row and the header carry the mark, a note says what to do,
    // and the editor's save, run and remove are off.
    const mark = page.getByText('No slash command', { exact: true })
    await mark.nth(1).waitFor()
    if ((await mark.count()) !== 2) throw new Error('the no-command mark is not on the row and the header')
    await page.getByText("A removed workflow holds this workflow's ID and slash-command name").waitFor()
    if ((await page.getByText(/^\/workflow (debug-project|wfd_debug)$/).count()) !== 0) {
      throw new Error('a command is offered for a workflow no ref reaches')
    }
    for (const name of ['Save new revision', 'Run workflow', REMOVE]) {
      if (!(await page.getByRole('button', { name }).isDisabled())) {
        throw new Error(`${name} is not disabled for a workflow no ref reaches`)
      }
    }
    await page.getByText('/workflow triage-tickets').first().waitFor()
    await shoot(page, `${OUT}/reserved-both-${theme}.png`)

    await page.close()
  }
} finally {
  await browser.close()
}
console.log(`wrote frames to ${OUT}`)
