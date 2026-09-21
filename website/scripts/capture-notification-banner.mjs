/**
 * Screenshot + recording harness for the in-app notification banner and the
 * system-notification permission surfaces.
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi) and
 * drives the /api/ws socket with Playwright's routeWebSocket, pushing
 * `notification` frames into the page exactly as the gateway would. That is
 * the banner's only trigger, so nothing in the shipped bundle needs a debug
 * hook for this harness to reach it. The browser's `Notification` global is
 * replaced per page so the three permission states can be photographed.
 *
 * Frames (each in dark and light):
 *   banner-default          one default-priority card at rest under the top bar
 *   banner-actions          a card carrying two dashboard-internal actions
 *   banner-critical         a critical approval card (danger-tinted icon, Review)
 *   banner-deck             three pending cards as a deck: newest in full, two
 *                           blank shells behind, "2 more" pill
 *   banner-deck-expanded    the same three after the deck edge was clicked
 *   banner-inbox-line       six pending, expanded: four cards + "+2 more in
 *                           your inbox"
 *   autohide-1/2/3          static frame series of the auto-hide flow: card
 *                           arrived · held under the pointer past 6 s · gone
 *                           with the bell badge lit
 *   banner-mobile           the newest card alone at 390px
 *   settings-permission-*   Settings › Notifications › Desktop alerts in the
 *                           default / granted / denied permission states
 *   popover-hint            the bell popover's controls card with the hint row
 *   banner-ack-failed       a card whose action's mark-as-read the gateway
 *                           refused (500): still on screen, notice under it
 *   popover-hint-save-failed  the hint after a "Not now" whose localStorage
 *                           write threw: row stays, notice under it
 *   popover-approval-row    the bell popover holding a critical approval row
 *                           (Approve / Reject capsules, danger dot, no edge)
 *
 * Recordings (dark, mp4 + gif when ffmpeg is present):
 *   rec-default-autohide    slide-in, hover pause, then the travel into the bell
 *   rec-critical-stays      a critical card outliving the auto-hide delay, then
 *                           dismissed by its close control
 *   rec-deck-expand         three arrivals stacking, the deck expanding
 *
 * Usage: node scripts/capture-notification-banner.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, existsSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/notification-banner'
mkdirSync(OUT, { recursive: true })

const DESKTOP = { width: 1280, height: 800 }
const MOBILE = { width: 390, height: 844 }

let seq = 0
const note = (over = {}) => ({
  kind: 'cron', source: 'system', channel: 'system.cron', priority: 'default',
  title: 'Nightly digest finished', body: 'Three repos summarized, two PRs need a look.',
  ts: new Date(Date.now() - seq++ * 1000).toISOString(), acked: false, ...over,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

/**
 * A page against the built SPA with the socket held open for pushes.
 * `permission` replaces the platform `Notification` so the permission
 * surfaces render a chosen state; `seeded` pre-fills the inbox.
 */
async function openPage({ theme = 'dark', viewport = DESKTOP, permission = 'granted', seeded = [], record = null, ackStatus = 200, storageThrows = null } = {}) {
  const context = await browser.newContext({
    viewport, deviceScaleFactor: record ? 1 : 2,
    ...(record ? { recordVideo: { dir: record, size: viewport } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  let ws = null
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'GET') {
        await json(route, { notifications: seeded, unread: seeded.filter(n => !n.acked).length })
        return true
      }
      if (path === '/api/notifications/channels') { await json(route, { channels: [] }); return true }
      if (path === '/api/notifications/ack' && ackStatus !== 200) { await json(route, { error: 'unavailable' }, ackStatus); return true }
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
        return true
      }
      return false
    },
  })
  // Registered AFTER the stub so this handler wins and can push server frames.
  await page.routeWebSocket(/\/api\/ws/, socket => { ws = socket })
  await page.addInitScript((perm) => {
    // Like a real browser answering a prompt with no user gesture behind it
    // (useNativeNotification asks from an effect on the first unacked
    // arrival): the request resolves without changing the verdict, so the
    // photographed state is the one the harness asked for.
    class FakeNotification {
      static permission = perm
      static requestPermission() { return Promise.resolve(FakeNotification.permission) }
    }
    Object.defineProperty(window, 'Notification', { value: FakeNotification, configurable: true, writable: true })
  }, permission)
  if (storageThrows) {
    // A write to ONE key fails (a locked-down or full store), after the boot
    // seeds above have landed, so only the surface under test sees it.
    await page.addInitScript((key) => {
      const orig = Storage.prototype.setItem
      Storage.prototype.setItem = function (k, v) {
        if (k === key) throw new DOMException('quota', 'QuotaExceededError')
        return orig.call(this, k, v)
      }
    }, storageThrows)
  }
  await page.goto(base + '/')
  await page.locator('button:has(svg.lucide-bell)').waitFor({ state: 'visible', timeout: 20000 })
  // The socket binds during boot; without it no frame can reach the banner.
  for (let i = 0; i < 50 && !ws; i++) await page.waitForTimeout(100)
  if (!ws) throw new Error('websocket route never bound')
  await page.waitForTimeout(600)
  const push = n => ws.send(JSON.stringify({ type: 'notification', data: n }))
  return { context, page, push }
}

const card = page => page.getByTestId('notification-banner-card')
const shoot = (page, name) => page.screenshot({ path: join(OUT, `${name}.png`) })

// ---- stills -----------------------------------------------------------------
for (const theme of ['dark', 'light']) {
  {
    const { context, page, push } = await openPage({ theme })
    push(note())
    await card(page).first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(500)
    await shoot(page, `banner-default-${theme}`)
    console.log(`banner-default-${theme}: 1 card`)
    await context.close()
  }
  {
    const { context, page, push } = await openPage({ theme })
    push(note({
      kind: 'agent', title: 'Weekly cost report is ready',
      body: 'Spend is down 12% week over week; one anomaly on the EU stack.',
      actions: [{ id: 'runs', label: 'Open run', url: '/schedule' }, { id: 'report', label: 'View report', url: '/system' }],
    }))
    await card(page).first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(500)
    await shoot(page, `banner-actions-${theme}`)
    console.log(`banner-actions-${theme}: 2 actions`)
    await context.close()
  }
  {
    const { context, page, push } = await openPage({ theme })
    push(note({ kind: 'approval', priority: 'critical', channel: 'system.approval', title: 'Tool approval needed', body: 'shell: git push origin feat/notification-banner' }))
    await card(page).first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(500)
    if ((await page.getByTestId('notification-banner-region').getAttribute('role')) !== 'alert') throw new Error('critical card did not switch the region to alert')
    await shoot(page, `banner-critical-${theme}`)
    console.log(`banner-critical-${theme}: alert region`)
    await context.close()
  }
  {
    const { context, page, push } = await openPage({ theme })
    push(note({ title: 'Backup completed', body: 'Snapshot 2026-09-21 stored.' }))
    await page.waitForTimeout(350)
    push(note({ kind: 'subagent', title: 'Research subagent finished', body: 'Summary attached to the session.' }))
    await page.waitForTimeout(350)
    push(note({ kind: 'hook', title: 'Webhook received', body: 'GitHub: PR #12496 was approved.' }))
    await page.waitForFunction(() => document.querySelectorAll('[data-testid="notification-banner-card"]').length === 3, null, { timeout: 5000 })
    await page.waitForTimeout(600)
    // The deck must be blank shells: nothing but the top card's own text may
    // be on screen, and the pill must read "2 more".
    const shells = await page.getByTestId('notification-banner-deck-shell').evaluateAll(els => els.map(e => e.textContent))
    if (shells.length !== 2 || shells.some(t => t !== '')) throw new Error(`deck shells not blank: ${JSON.stringify(shells)}`)
    if ((await page.getByText('Backup completed').count()) !== 0) throw new Error('an older card\'s text is visible behind the deck')
    if ((await page.getByTestId('notification-banner-count').textContent()) !== 'Show 2 more') throw new Error('pill does not read "Show 2 more"')
    await shoot(page, `banner-deck-${theme}`)
    console.log(`banner-deck-${theme}: 3 cards, blank deck, "Show 2 more"`)
    // The deck edge is the strip peeking below the top card.
    const top = await card(page).first().boundingBox()
    await page.mouse.click(top.x + top.width / 2, top.y + top.height + 1)
    await page.waitForFunction(() => document.querySelectorAll('[data-testid="notification-banner-card"][data-deck]').length === 0, null, { timeout: 5000 })
    await page.waitForTimeout(600)
    await shoot(page, `banner-deck-expanded-${theme}`)
    console.log(`banner-deck-expanded-${theme}: list`)
    await context.close()
  }
  {
    const { context, page, push } = await openPage({ theme })
    const kinds = ['cron', 'subagent', 'hook', 'agent', 'cron', 'heartbeat']
    for (let i = 0; i < 6; i++) { push(note({ kind: kinds[i], title: `Pending note ${i + 1}`, body: `Body of note ${i + 1}.` })); await page.waitForTimeout(250) }
    await page.waitForFunction(() => document.querySelectorAll('[data-testid="notification-banner-card"]').length === 3, null, { timeout: 5000 })
    await page.getByTestId('notification-banner-count').click()
    await page.getByText('+2 more in your inbox').waitFor({ timeout: 5000 })
    await page.waitForTimeout(600)
    if ((await card(page).count()) !== 4) throw new Error('expanded list must cap at four cards')
    await shoot(page, `banner-inbox-line-${theme}`)
    console.log(`banner-inbox-line-${theme}: 4 cards + inbox line`)
    await context.close()
  }
  {
    const { context, page, push } = await openPage({ theme, viewport: MOBILE })
    push(note({ title: 'Older note', body: 'Should not show on mobile.' }))
    await page.waitForTimeout(300)
    push(note({ kind: 'agent', title: 'Your agent left a note', body: 'The deploy finished while you were away.' }))
    await card(page).first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(600)
    if ((await card(page).count()) !== 1) throw new Error('mobile must show exactly one card')
    // Touch has no hover: the close must be visible at rest.
    const xOpacity = await page.getByTestId('notification-banner-dismiss').evaluate(el => getComputedStyle(el).opacity)
    if (Number(xOpacity) < 0.5) throw new Error(`mobile close is not resting-visible (opacity ${xOpacity})`)
    await shoot(page, `banner-mobile-${theme}`)
    console.log(`banner-mobile-${theme}: 1 card full width`)
    await context.close()
  }
  for (const permission of ['default', 'granted', 'denied']) {
    const { context, page } = await openPage({ theme, permission })
    await page.goto(`${base}/settings?tab=notifications`, { waitUntil: 'domcontentloaded' })
    const row = page.getByTestId('system-notifications-row')
    await row.waitFor({ timeout: 20000 })
    await page.waitForTimeout(800)
    // The whole Desktop alerts card: the permission row plus the banner toggle.
    const cardEl = row.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
    await cardEl.screenshot({ path: join(OUT, `settings-permission-${permission}-${theme}.png`) })
    console.log(`settings-permission-${permission}-${theme}`)
    await context.close()
  }
  {
    const { context, page, push } = await openPage({ theme, ackStatus: 500 })
    push(note({ kind: 'agent', title: 'Weekly cost report is ready', body: 'Spend is down 12% week over week.', actions: [{ id: 'report', label: 'View report', url: '/system' }] }))
    await card(page).first().waitFor({ timeout: 5000 })
    await page.getByRole('button', { name: 'View report' }).click()
    await page.getByTestId('notification-banner-ack-failed').waitFor({ timeout: 5000 })
    if ((await card(page).count()) !== 1) throw new Error('card must stay on a refused ack')
    // Hover so the auto-hide clock holds while the frame is taken.
    const box = await card(page).first().boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.waitForTimeout(400)
    await shoot(page, `banner-ack-failed-${theme}`)
    console.log(`banner-ack-failed-${theme}: card kept, notice shown`)
    await context.close()
  }
  {
    const seeded = [note({ title: 'Nightly digest finished' })]
    const { context, page } = await openPage({ theme, permission: 'default', seeded, storageThrows: 'mc-notification-permission-hint-dismissed' })
    await page.locator('button:has(svg.lucide-bell)').click()
    await page.getByTestId('notification-permission-hint').waitFor({ timeout: 5000 })
    await page.getByRole('button', { name: 'Not now' }).click()
    await page.getByTestId('notification-permission-hint-save-failed').waitFor({ timeout: 5000 })
    await page.waitForTimeout(500)
    await shoot(page, `popover-hint-save-failed-${theme}`)
    console.log(`popover-hint-save-failed-${theme}: row kept, notice shown`)
    await context.close()
  }
  {
    const seeded = [
      note({ kind: 'approval', priority: 'critical', channel: 'system.approval', title: 'Tool approval needed', body: 'shell: git push origin feat/notification-banner', approval_id: 'appr-1' }),
      note({ title: 'Nightly digest finished' }),
    ]
    const { context, page } = await openPage({ theme, seeded })
    await page.locator('button:has(svg.lucide-bell)').click()
    await page.getByRole('button', { name: 'Approve' }).waitFor({ timeout: 5000 })
    await page.waitForTimeout(700)
    await shoot(page, `popover-approval-row-${theme}`)
    console.log(`popover-approval-row-${theme}: Approve/Reject capsules`)
    await context.close()
  }
  {
    const seeded = [note({ title: 'Nightly digest finished' }), note({ kind: 'agent', title: 'Agent note' })]
    const { context, page } = await openPage({ theme, permission: 'default', seeded })
    await page.locator('button:has(svg.lucide-bell)').click()
    const hint = page.getByTestId('notification-permission-hint')
    try {
      await hint.waitFor({ timeout: 5000 })
    } catch (err) {
      // Name what the frame would have shown instead of a bare timeout.
      const phase = await page.locator('[data-nc-phase]').getAttribute('data-nc-phase').catch(() => 'none')
      const rows = await page.locator('[data-notif-row]').count()
      const perm = await page.evaluate(() => Notification.permission)
      throw new Error(`popover hint missing: nc-phase=${phase} rows=${rows} permission=${perm} (${err.message.split('\n')[0]})`)
    }
    await page.waitForTimeout(700)
    await shoot(page, `popover-hint-${theme}`)
    console.log(`popover-hint-${theme}`)
    await context.close()
  }
}

// ---- auto-hide frame series (for readers who cannot play the GIF) ----------
{
  const { context, page, push } = await openPage({ theme: 'dark' })
  push(note())
  await card(page).first().waitFor({ timeout: 5000 })
  await page.waitForTimeout(500)
  await shoot(page, 'autohide-1-arrived')
  const box = await card(page).first().boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.waitForTimeout(6800)
  if ((await card(page).count()) !== 1) throw new Error('hover did not pause auto-hide')
  await shoot(page, 'autohide-2-held-under-pointer')
  await page.mouse.move(box.x + box.width / 2, box.y + box.height + 200)
  await card(page).first().waitFor({ state: 'detached', timeout: 9000 })
  await page.locator('button[aria-label]:has(svg.lucide-bell) span[aria-hidden="true"]').waitFor({ timeout: 3000 })
  await page.waitForTimeout(400)
  await shoot(page, 'autohide-3-gone-badge-lit')
  console.log('autohide-1/2/3: frame series')
  await context.close()
}

// ---- recordings -------------------------------------------------------------
const REC_DIR = join(OUT, 'rec-raw')
mkdirSync(REC_DIR, { recursive: true })

async function recordFlow(name, run) {
  const { context, page, push } = await openPage({ theme: 'dark', record: REC_DIR })
  const video = page.video()
  await run(page, push)
  await page.waitForTimeout(600)
  await context.close()
  const webm = await video.path()
  const dest = join(OUT, `${name}.webm`)
  renameSync(webm, dest)
  console.log(`${name}: ${dest}`)
  encode(dest, name)
}

function encode(webm, name) {
  const has = spawnSync('ffmpeg', ['-version'], { encoding: 'utf-8' })
  if (has.error) { console.log('ffmpeg absent — webm only'); return }
  const mp4 = join(OUT, `${name}.mp4`)
  spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', webm, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', mp4])
  const gif = join(OUT, `${name}.gif`)
  spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', webm,
    '-vf', 'fps=12,scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3',
    gif])
  console.log(`  mp4 ${existsSync(mp4)} gif ${existsSync(gif)}`)
}

await recordFlow('rec-default-autohide', async (page, push) => {
  await page.waitForTimeout(500)
  push(note())
  await card(page).first().waitFor({ timeout: 5000 })
  await page.waitForTimeout(1200)
  // Hover pauses the auto-hide clock: well past the delay, the card stays.
  const box = await card(page).first().boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.waitForTimeout(6800)
  if ((await card(page).count()) !== 1) throw new Error('hover did not pause auto-hide')
  await page.mouse.move(box.x + box.width / 2, box.y + box.height + 200)
  await card(page).first().waitFor({ state: 'detached', timeout: 9000 })
  // The bell's unread badge is lit: auto-hide left the note unread.
  await page.locator('button[aria-label]:has(svg.lucide-bell) span[aria-hidden="true"]').waitFor({ timeout: 3000 })
  await page.waitForTimeout(900)
})

await recordFlow('rec-critical-stays', async (page, push) => {
  await page.waitForTimeout(500)
  push(note({ kind: 'approval', priority: 'critical', channel: 'system.approval', title: 'Tool approval needed', body: 'shell: git push origin feat/notification-banner' }))
  await card(page).first().waitFor({ timeout: 5000 })
  await page.waitForTimeout(7200)
  if ((await card(page).count()) !== 1) throw new Error('critical card auto-hid')
  const box = await card(page).first().boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.waitForTimeout(500)
  await page.getByTestId('notification-banner-dismiss').click()
  await card(page).first().waitFor({ state: 'detached', timeout: 5000 })
  await page.waitForTimeout(700)
})

await recordFlow('rec-deck-expand', async (page, push) => {
  await page.waitForTimeout(500)
  push(note({ title: 'Backup completed', body: 'Snapshot 2026-09-21 stored.' }))
  await page.waitForTimeout(700)
  push(note({ kind: 'subagent', title: 'Research subagent finished', body: 'Summary attached to the session.' }))
  await page.waitForTimeout(700)
  push(note({ kind: 'hook', title: 'Webhook received', body: 'GitHub: PR #12496 was approved.' }))
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="notification-banner-card"]').length === 3, null, { timeout: 5000 })
  await page.waitForTimeout(1200)
  const top = await card(page).first().boundingBox()
  await page.mouse.move(top.x + top.width / 2, top.y + top.height + 1)
  await page.waitForTimeout(400)
  await page.mouse.click(top.x + top.width / 2, top.y + top.height + 1)
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="notification-banner-card"][data-deck]').length === 0, null, { timeout: 5000 })
  await page.waitForTimeout(1500)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(700)
})

rmSync(REC_DIR, { recursive: true, force: true })
await browser.close()
srv.close()
console.log('OK — frames written to', OUT)
