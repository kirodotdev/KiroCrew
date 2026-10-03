/**
 * Frames of the composer spawn-approval banner's gone state (#14711): a press
 * on a spawn whose approval is gone says so through ErrorNotice and withdraws
 * that spawn's Approve/Reject instead of doing nothing on screen.
 *
 * Drives the isolated capture entry (website/capture/spawn-approval-gone.html),
 * which mounts the REAL useSpawnApprovals hook and SpawnApprovalCard. Frames:
 *   19: three spawns; the server answers the middle one's Approve with a 404.
 *       That row's buttons are replaced by the notice on its own line and its
 *       name is struck through as settled; the other two keep theirs, and the
 *       headline counts the two still awaiting approval.
 *   20: one spawn with no live request (no target); Approve is refused on the
 *       client, and the spawn's own row shows the notice under its name,
 *       beside Review in panel (light theme). Nothing is announced as
 *       awaiting, and the banner drops its glow.
 *   21: two spawns; the server answers one Approve with a retryable 503. Every
 *       button stays, and one notice under the banner quotes the reason.
 * Each frame asserts the notice text and the button counts before it is written.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-spawn-approval-gone.mjs http://127.0.0.1:6822 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const GONE = 'This approval has expired or was already decided'
const RETRY = "This decision wasn't recorded"
const browser = await chromium.launch()
let failed = false

const frames = [
  { name: '19-spawn-banner-404-row', theme: 'dark', query: 'count=3', press: { name: 'Approve sub-agent: Draft the release notes' }, rowButtons: 4, notice: 'spawn-approval-gone-row', headline: '2 sub-agents are awaiting your approval to run' },
  { name: '20-spawn-banner-targetless-light', theme: 'light', query: 'count=1&targetless=1', press: { name: /^Approve$/ }, rowButtons: 0, notice: 'spawn-approval-gone-row', headline: null, goneName: 'Summarise the deploy logs' },
  { name: '21-spawn-banner-retryable', theme: 'dark', query: 'count=2', press: { name: 'Approve sub-agent: Summarise the deploy logs' }, rowButtons: 4, notice: 'spawn-approval-error', headline: '2 sub-agents are awaiting your approval to run', status: 503, expect: RETRY },
]

for (const f of frames) {
  const page = await browser.newPage({ viewport: { width: 760, height: f.rowButtons ? 300 : 180 }, colorScheme: f.theme })
  const decides = []
  // Only the gateway's own routes, never the dev server's module paths.
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    decides.push(route.request().url())
    return route.fulfill({ status: f.status ?? 404, contentType: 'application/json', body: JSON.stringify({ error: f.status ? 'gateway busy, try again' : 'no pending approval' }) })
  })
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${f.theme}&${f.query}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByRole('button', f.press).click()
  const notice = page.getByTestId(f.notice)
  await notice.waitFor()
  await page.waitForTimeout(500)
  const text = await notice.innerText()
  const rowButtons = await page.getByRole('button', { name: /^(Approve|Reject) sub-agent:/ }).count()
  const header = await page.getByRole('button', { name: /^(Approve|Reject)( all)?$/ }).count()
  const expectHeader = f.rowButtons ? 2 : 0
  const expectDecides = f.rowButtons ? 1 : 0
  const awaiting = await page.getByText(/awaiting your approval/).count()
  const headlineOk = f.headline ? (await page.getByText(f.headline).count()) === 1 : awaiting === 0
  // The gone spawn's own row names it, above its notice.
  const goneNameOk = f.goneName ? (await page.locator('[data-gone]').innerText()).includes(f.goneName) : true
  // No Dismiss anywhere, and a gone row reads as settled.
  const dismiss = await page.getByTestId('spawn-approval-card').getByRole('button', { name: /Dismiss/ }).count()
  const settledRows = await page.locator('[data-gone]').count()
  const goneRow = f.notice === 'spawn-approval-gone-row' ? 1 : 0
  const shapeOk = dismiss === 0 && settledRows === goneRow
  const ok = text.includes(f.expect ?? GONE) && rowButtons === f.rowButtons && header === expectHeader && decides.length === expectDecides && headlineOk && goneNameOk && shapeOk
  console.log(`${f.name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ text, rowButtons, header, decides: decides.length, headlineOk, goneNameOk, dismiss, settledRows })}`)
  if (ok) await page.screenshot({ path: `${OUT}/${f.name}.png` })
  else failed = true
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
