/**
 * Before / after for "a chained crew's long name stays inside its chip".
 *
 * The crew on screen is reached through another crew, so its chip carries two
 * boxes: the parent's name, then the crew's own. The own name used to be
 * `shrink-0` with no cap, so a name longer than the 140px label box overflowed it,
 * ran past the chip's border and under the switcher trigger's unread badge.
 *
 * Runs against the REAL built SPA with a stubbed API. The parent crew's pane posts
 * an unread count, so the trigger carries its badge as in the report. `before` is
 * reproduced by injecting the old declaration back onto the name box, so both
 * sides come from one build and differ only in that one rule.
 *
 * Assertions, per width, after:
 *  - the crew's name box ends inside the chip's content box
 *  - the chip ends before the switcher trigger starts
 *  - the name is ellipsised (its text is wider than its box), and the chip's
 *    `title` still carries the full name
 * and before reproduces the defect at every width (the name runs past the chip).
 *
 * Usage: npm run build && node scripts/capture-crew-chip-long-name.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-chip-long-name'
mkdirSync(OUT, { recursive: true })

/** The old rule: the crew's own name neither shrinks nor is capped. */
const BEFORE_CSS = '.tb-crew-active-chip .tb-drop-crew-name > span:last-child{max-width:none}'
const LONG = 'Cloud Crew With A Long Name (cc-a1b2c3)'

const crew = (id, name, port, extra = {}) => ({
  id, name, ssh_host: `${id}-host`, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: true,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
  ...extra,
})
const CREWS = [
  crew('main', 'Main Crew', 7801),
  crew('cloud', LONG, 7802, { via_instance_id: 'main', via_remote_port: 7778 }),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const SLOTS = [{
  key: 'crew-chip-long-name', title: 'Long crew name in the header chip', running: false,
  last_message: '', messages: 2, agent: 'kirocrew', memory_mode: 'persistent',
  folder_id: '', modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]

/** A wide window, a mid one, and a narrow one. */
const WIDTHS = [1280, 900, 640]
const TRIGGER = '[aria-label^="Switch crew"] >> visible=true'
const CHIP = '.tb-crew-active-chip >> visible=true'

// The bar on screen: while a pane is up, the local header is hidden and the
// pane's own strip carries the chip, so pick whichever chip has a box.
const VISIBLE_CHIP = () => {
  window.visibleCrewChip = () => [...document.querySelectorAll('.tb-crew-active-chip')]
    .find((b) => b.getBoundingClientRect().width > 0)
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const extra = async (path, route) => {
  if (path === '/api/instances') {
    await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
    return true
  }
  const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token)$/.exec(path)
  if (tunnel) {
    const found = CREWS.find((c) => c.id === decodeURIComponent(tunnel[1]))
    await json(route, { ...(found ? found.status : { state: 'connected' }), token: 'stub-token' })
    return true
  }
  if (path.startsWith('/api/instances/')) {
    await json(route, { ok: true })
    return true
  }
  return false
}

// The parent crew's pane keeps reporting 5 unread, as a real pane does.
const PARENT_PANE = '<!doctype html><title>pane</title><script>' +
  'setInterval(()=>parent.postMessage({type:"mc-unread-slots",count:5},"*"),200)</script>'

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function pick(page, name) {
  await page.locator(TRIGGER).first().click()
  await page.getByRole('menuitemradio', { name }).first().click()
}

async function shot(width, state) {
  const context = await browser.newContext({ viewport: { width, height: 300 } })
  const page = await context.newPage()
  await page.route(/127\.0\.0\.1:7801/, (route) =>
    route.fulfill({ contentType: 'text/html', body: PARENT_PANE }))
  await page.route(/127\.0\.0\.1:7802/, (route) =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
  await page.addInitScript(VISIBLE_CHIP)
  await stubDashboardApi(page, { theme: 'dark', slots: SLOTS, extra })
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(TRIGGER, { timeout: 20000 })
  await pick(page, /^Main Crew/)
  await page.waitForSelector(CHIP, { timeout: 10000 })
  await pick(page, /Cloud Crew With/)
  await page.waitForFunction(
    (long) => window.visibleCrewChip()?.getAttribute('title')?.startsWith(long),
    LONG, { timeout: 10000 })
  await page.waitForFunction(
    () => /\d/.test(window.visibleCrewChip()?.closest('.instance-tab-bar, .instance-tab-bar-inline')
      ?.querySelector('[aria-label^="Switch crew"]')?.textContent ?? ''),
    null, { timeout: 10000 })
  if (state === 'before') await page.addStyleTag({ content: BEFORE_CSS })
  await page.waitForTimeout(250)

  const m = await page.evaluate(() => {
    const chip = window.visibleCrewChip()
    const trig = chip.closest('.instance-tab-bar, .instance-tab-bar-inline')
      .querySelector('[aria-label^="Switch crew"]')
    const own = chip.querySelector('.tb-drop-crew-name > span:last-child')
    const cs = getComputedStyle(chip)
    const cr = chip.getBoundingClientRect()
    const innerRight = cr.right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth)
    const or = own.getBoundingClientRect()
    return {
      title: chip.getAttribute('title') ?? '',
      ownRight: +or.right.toFixed(1),
      innerRight: +innerRight.toFixed(1),
      chipRight: +cr.right.toFixed(1),
      triggerLeft: +trig.getBoundingClientRect().left.toFixed(1),
      ellipsised: own.scrollWidth > own.clientWidth + 0.5,
      // Where the painted text ends: the box's left plus the text's full width.
      textRight: +(or.left + own.scrollWidth).toFixed(1),
    }
  })
  await page.screenshot({ path: `${OUT}/${state}-${width}.png`, clip: { x: 0, y: 0, width, height: 46 } })
  await context.close()
  return m
}

for (const width of WIDTHS) {
  const before = await shot(width, 'before')
  const after = await shot(width, 'after')
  console.log(`\n=== ${width}px ===`)
  for (const [name, m] of [['before', before], ['after', after]]) console.log(`  ${name}: ${JSON.stringify(m)}`)
  if (before.textRight <= before.chipRight) {
    fail(`${width}px: before keeps the name inside the chip, so this pair does not reproduce the defect`)
  }
  if (after.ownRight > after.innerRight + 0.5) {
    fail(`${width}px: the name box ends at ${after.ownRight}, past the chip's content edge ${after.innerRight}`)
  }
  if (after.chipRight > after.triggerLeft + 0.5) {
    fail(`${width}px: the chip ends at ${after.chipRight}, over the trigger at ${after.triggerLeft}`)
  }
  if (!after.ellipsised) fail(`${width}px: the long name is not ellipsised`)
  if (!after.title.includes(LONG)) fail(`${width}px: the chip's title lost the full name`)
}

await browser.close()
srv.close()
if (failures) {
  console.error(`\n${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('\nALL GREEN')
