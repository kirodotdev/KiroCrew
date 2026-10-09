/**
 * Frames for three refusals a live run shows on surfaces outside the chat
 * (#16389). Each frame asserts its text and controls before it is written.
 *   26-project-gate-refused-dag: a running project with two tasks at their
 *       gates. Task 2's Approve is refused by the server as gone; the page's
 *       error notice names task 2 as no longer waiting, in the DAG view, and the page
 *       reads the listing again, which no longer holds task 2's gate: its
 *       banner and its Approve/Deny are gone, and task 1's live gate keeps
 *       its own.
 *   28-subagent-pane-no-target: a sub-agent card in the Subagents panel whose
 *       spawn names no request. Its Approve sends nothing, the buttons are
 *       withdrawn, the header reads "No longer pending" in place of "Pending
 *       Approval" and the card shows the shared sentence.
 *   29-approval-entry-refused: the panel's spawn approval entry after a
 *       refused press: no buttons, a "No longer pending" header under a neutral
 *       icon in place of "Approval Needed", and the shared sentence.
 *
 * Drives the isolated capture entries website/capture/project-gate-refused.html
 * and website/capture/spawn-approval-trust.html (`subagent=1`), which mount the
 * REAL ProjectDetailPage and ActivityViewer.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-gate-and-subagent-refusal.mjs http://127.0.0.1:6822 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const GONE = 'This approval has expired or was already decided'
const TASK2_GONE = 'Task 2 "Rotate the API keys" is no longer waiting for your decision'
const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify(detail)}`)
  if (!ok) failed = true
  return ok
}

// Gateway-free: the listing names both tasks' gates live; any decide is refused as the gateway refuses a gone approval, and
// from then on the listing no longer holds the refused gate, as the gateway's
// would not.
async function stubProject(page) {
  const decides = []
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/approvals') {
      const listing = [
        { id: 'task-gate-run-1-1-aaaa', source: 'taskrunner', slot: '', instance: 'inst-1' },
        { id: 'task-gate-run-1-2-bbbb', source: 'taskrunner', slot: '', instance: 'inst-2' },
      ]
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(
        decides.length ? listing.filter(a => a.id !== 'task-gate-run-1-2-bbbb') : listing) })
    }
    if (path.startsWith('/api/approvals/')) {
      decides.push(path)
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"error": "not found or expired"}' })
    }
    const isList = /commands|skills|agents|sessions|files|history|models|tasks|runs/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  return decides
}

async function projectPage() {
  const page = await browser.newPage({ viewport: { width: 1100, height: 640 } })
  const decides = await stubProject(page)
  await page.goto(`${BASE}/capture/project-gate-refused.html?theme=dark`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Rotate the API keys').first().waitFor()
  await page.waitForTimeout(800)
  return { page, decides }
}

{
  const { page, decides } = await projectPage()
  const node = page.locator('[data-task-index="2"], [data-id="2"]').first()
  const approve = (await node.count())
    ? node.getByRole('button', { name: /^Approve/ }).first()
    : page.getByRole('button', { name: /^Approve/ }).last()
  await approve.click()
  const notice = page.getByTestId('project-detail-action-error')
  await notice.waitFor()
  await page.getByText('Rotate the API keys" is waiting for your decision').waitFor({ state: 'detached' })
  await page.waitForTimeout(400)
  const s = {
    notice: await notice.innerText(), role: await notice.getAttribute('role'), decides: decides.length,
    waitingBanner: await page.getByText('Rotate the API keys" is waiting for your decision').count(),
    // Task 1's gate is still live, so its one Approve remains.
    approve: await page.getByRole('button', { name: /^Approve/ }).count(),
  }
  if (check('26-project-gate-refused-dag', s.notice.includes(TASK2_GONE) && s.decides === 1 && s.waitingBanner === 0 && s.approve === 1, s)) {
    await page.screenshot({ path: `${OUT}/26-project-gate-refused-dag.png` })
  }
  await page.close()
}

{
  const page = await browser.newPage({ viewport: { width: 640, height: 560 } })
  await page.goto(`${BASE}/capture/spawn-approval-trust.html?theme=dark&subagent=1`)
  await page.waitForSelector('[data-capture-root]')
  const card = page.locator('[data-capture-root]').getByText('Audit the release notes for the insider build').first()
  await card.waitFor()
  await page.waitForTimeout(400)
  const before = await page.getByRole('button', { name: /^Approve/ }).count()
  const pendingBefore = await page.getByText('Pending Approval', { exact: true }).count()
  await page.getByRole('button', { name: /^Approve/ }).last().click()
  await page.getByText(GONE).first().waitFor()
  await page.waitForTimeout(400)
  const s = {
    before, after: await page.getByRole('button', { name: /^Approve/ }).count(), notice: await page.getByText(GONE).count(),
    pendingBefore, pendingAfter: await page.getByText('Pending Approval', { exact: true }).count(),
    settled: await page.getByText('No longer pending', { exact: true }).count(),
  }
  if (check('28-subagent-pane-no-target', s.notice >= 1 && s.after === s.before - 1 && s.pendingAfter === s.pendingBefore - 1 && s.settled === 1, s)) {
    await page.screenshot({ path: `${OUT}/28-subagent-pane-no-target.png` })
  }
  await page.close()
}

{
  const page = await browser.newPage({ viewport: { width: 640, height: 420 } })
  await page.goto(`${BASE}/capture/spawn-approval-trust.html?theme=dark&refuse=404`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Approval Needed', { exact: true }).waitFor()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText(GONE).first().waitFor()
  await page.waitForTimeout(400)
  const s = {
    buttons: await page.getByRole('button', { name: /^(Approve|Reject)/ }).count(),
    needed: await page.getByText('Approval Needed', { exact: true }).count(),
    settled: await page.getByText('No longer pending', { exact: true }).count(),
    notice: await page.getByText(GONE).count(),
  }
  if (check('29-approval-entry-refused', s.buttons === 0 && s.needed === 0 && s.settled === 1 && s.notice === 1, s)) {
    await page.screenshot({ path: `${OUT}/29-approval-entry-refused.png` })
  }
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
