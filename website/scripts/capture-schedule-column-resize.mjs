/**
 * Evidence harness for the Schedule jobs table's user-resizable columns.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception: gateway-free, no kiro-cli, no dashboard auth. It cannot run
 * against the Vite dev server: the stub answers every path containing `/api/`,
 * which in dev includes the app's own `/src/api/*.ts` modules.
 *
 * It MEASURES as well as photographs, because the property at stake is
 * arithmetic a screenshot cannot show: the jobs table is `table-fixed`, so a
 * column that grows without the table's min-width growing by the same amount
 * takes its pixels out of Message and draws Message over Status. Each step
 * prints the header widths, the table width and the Message residual, and the
 * script exits non-zero when a resize was not exact or Message lost its floor.
 *
 * The fixture ids and names are deliberately longer than the 68px / 160px
 * defaults: the point of the feature is reading a value the default truncates.
 *
 * Usage: node scripts/capture-schedule-column-resize.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/schedule-column-resize'
const PREFIX = process.argv[3] || 'after'

/** Same number as MESSAGE_FLOOR in SchedulePage.columnContract.test.ts. */
const MESSAGE_FLOOR = 176

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const JOBS = [
  {
    id: 'a3f9c2e1-7b40', name: 'Nightly CI failure digest for the build-health channel', schedule: 'every 1d',
    timezone: 'America/Los_Angeles', message: 'Summarise yesterday\'s CI failures and post the digest to #build-health.',
    enabled: true, agent: 'kirocrew', last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200,
  },
  {
    id: 'c81d04ab-22f6', name: 'Upstream feed poller (staging mirror)', schedule: 'every 300s', enabled: true,
    script: '~/.kiro/crew/crons/feed.py:check', message: '', last_status: 'error',
    last_error: 'HTTP 502 from upstream', last_run_ts: now - 240, next_run_ts: now + 60,
  },
  {
    id: 'e7720b9d-915c', name: 'Disk check', schedule: '0 9 * * 1', enabled: false,
    command: 'df -h / | tail -1', message: '', last_run_ts: now - 86400 * 2, next_run_ts: null,
  },
]

/** Header widths by visible label, plus the table's own box. */
async function measure(page) {
  return page.evaluate(() => {
    const table = document.querySelector('table')
    const widths = {}
    for (const th of table.querySelectorAll('thead th')) {
      const label = (th.querySelector('button, span')?.textContent || th.textContent || '').trim() || '(select)'
      widths[label] = Math.round(th.getBoundingClientRect().width)
    }
    const scroller = table.parentElement
    return {
      widths,
      table: Math.round(table.getBoundingClientRect().width),
      scrollerClient: scroller.clientWidth,
      scrollerScroll: scroller.scrollWidth,
    }
  })
}

async function dragGrip(page, column, dx) {
  const grip = page.getByRole('separator', { name: `Resize column: ${column}` })
  const box = await grip.boundingBox()
  const x = box.x + box.width / 2
  const y = box.y + box.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  // Several moves, not one jump: the width is applied per pointermove, and a
  // single event would not exercise the live path the user sees.
  for (let i = 1; i <= 8; i++) await page.mouse.move(x + (dx * i) / 8, y)
  await page.mouse.up()
  await page.waitForTimeout(150)
}

const failures = []
function check(ok, message) {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`)
  if (!ok) failures.push(message)
}

const VIEWPORT = { width: 1500, height: 700 }

/** A fresh, empty-storage context on the stubbed Schedule page, in one theme. */
async function newSchedulePage(browser, { theme, videoDir }) {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 1,
    ...(videoDir ? { recordVideo: { dir: videoDir, size: VIEWPORT } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    // The stub clears localStorage on every navigation by default, which would
    // make "the widths survive a reload" fail for a reason that is the
    // harness's, not the feature's. A fresh browser context starts empty anyway.
    preserveStorage: true,
    extra: async (path, route) => {
      if (path === '/api/crons') { await json(route, { jobs: JOBS }); return true }
      if (path === '/api/cron-folders') { await json(route, []); return true }
      if (path === '/api/crons/history') { await json(route, { runs: [] }); return true }
      if (path === '/api/agents') { await json(route, { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }); return true }
      if (path === '/api/models') { await json(route, []); return true }
      return false
    },
  })
  return { context, page }
}

/**
 * A recording shows no pointer: Playwright drives input below the compositor,
 * so a drag would read as a column widening by itself. This draws one that
 * follows the real pointer events, and turns accent while a button is held so
 * the press and the release are visible too.
 */
const FAKE_CURSOR = () => {
  const install = () => {
    const dot = document.createElement('div')
    dot.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;width:18px;height:18px;'
      + 'margin:-9px 0 0 -9px;border-radius:50%;border:2px solid #fff;background:rgba(120,120,120,.55);'
      + 'box-shadow:0 0 0 1px rgba(0,0,0,.6);transition:background .1s, transform .1s;left:-50px;top:-50px'
    document.body.appendChild(dot)
    const move = (e) => { dot.style.left = `${e.clientX}px`; dot.style.top = `${e.clientY}px` }
    const press = (down) => () => {
      dot.style.background = down ? 'rgba(140,90,255,.95)' : 'rgba(120,120,120,.55)'
      dot.style.transform = down ? 'scale(.8)' : 'scale(1)'
    }
    window.addEventListener('pointermove', move, true)
    window.addEventListener('pointerdown', press(true), true)
    window.addEventListener('pointerup', press(false), true)
  }
  if (document.body) install()
  else window.addEventListener('DOMContentLoaded', install)
}

/** A drag slow enough to follow by eye, for the recording only. */
async function slowDrag(page, column, dx) {
  const box = await page.getByRole('separator', { name: `Resize column: ${column}` }).boundingBox()
  const x = box.x + box.width / 2
  const y = box.y + box.height / 2
  await page.mouse.move(x - 120, y + 90, { steps: 1 })
  await page.mouse.move(x, y, { steps: 25 })
  await page.waitForTimeout(500)
  await page.mouse.down()
  await page.waitForTimeout(250)
  await page.mouse.move(x + dx, y, { steps: 60 })
  await page.waitForTimeout(350)
  await page.mouse.up()
  await page.waitForTimeout(700)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const { page } = await newSchedulePage(browser, { theme: 'dark' })

  const openOn = async (target) => {
    await target.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
    await target.getByRole('table').waitFor({ timeout: 60000 })
    await target.getByText('Disk check').first().waitFor()
    await target.waitForTimeout(400)
  }
  const open = () => openOn(page)

  await open()
  const before = await measure(page)
  console.log('default ', JSON.stringify(before))
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-default-dark.png` })
  check(before.widths.ID === 68 && before.widths.Name === 160, 'an untouched table renders the declared 68px / 160px')

  // The grip's resting rule appears while the header is hovered.
  await page.getByRole('columnheader', { name: 'Name', exact: true }).hover()
  await page.waitForTimeout(250)
  await page.locator('thead').first().screenshot({ path: `${OUT}/${PREFIX}-2-header-hover.png` })

  await dragGrip(page, 'ID', 60)
  await dragGrip(page, 'Name', 200)
  const wide = await measure(page)
  console.log('widened ', JSON.stringify(wide))
  await page.mouse.move(700, 600)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-3-widened-dark.png` })
  check(wide.widths.ID === 128, `ID tracked the pointer exactly (68 + 60 = 128, got ${wide.widths.ID})`)
  check(wide.widths.Name === 360, `Name tracked the pointer exactly (160 + 200 = 360, got ${wide.widths.Name})`)
  check(wide.widths.Message >= MESSAGE_FLOOR, `Message kept its ${MESSAGE_FLOOR}px floor (got ${wide.widths.Message})`)
  for (const col of ['Type', 'Schedule', 'Status', 'Last Run', 'Next Run', 'Actions']) {
    check(wide.widths[col] === before.widths[col], `${col} was not squeezed to pay for it (${before.widths[col]} -> ${wide.widths[col]})`)
  }

  // A narrow container is where the min-width arithmetic is load-bearing: the
  // table must scroll rather than void Message.
  await page.setViewportSize({ width: 1100, height: 700 })
  await page.waitForTimeout(400)
  const narrow = await measure(page)
  console.log('narrow  ', JSON.stringify(narrow))
  await page.screenshot({ path: `${OUT}/${PREFIX}-4-narrow-viewport.png` })
  check(narrow.widths.Message >= MESSAGE_FLOOR, `Message kept its floor in a narrow container (got ${narrow.widths.Message})`)
  check(narrow.widths.ID === 128 && narrow.widths.Name === 360, 'the resized widths held in a narrow container')
  check(narrow.scrollerScroll > narrow.scrollerClient, 'the table scrolls instead of squeezing a column')
  // Columns now scroll UNDER the pinned Actions header, and their grips go with
  // them. A grip with a z-index of its own draws through that header (and
  // steals its pointer events), which no width measurement would notice.
  const pinned = await page.evaluate(() => {
    const actions = [...document.querySelectorAll('thead th')].at(-1).getBoundingClientRect()
    const under = [...document.querySelectorAll('[data-testid="column-resizer"]')].filter((grip) => {
      const r = grip.getBoundingClientRect()
      const x = r.left + r.width / 2
      return x > actions.left && x < actions.right
    })
    const onTop = under.filter((grip) => {
      const r = grip.getBoundingClientRect()
      // `contains`, not `===`: the topmost node at the grip's centre is its
      // inner bar, so an identity test passes on the very build that is broken.
      return grip.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2))
    })
    return { under: under.length, onTop: onTop.length }
  })
  check(pinned.under > 0, `the fixture puts at least one grip under the pinned Actions header (${pinned.under})`)
  check(pinned.onTop === 0, `no grip paints over the pinned Actions header (${pinned.onTop} do)`)
  await page.setViewportSize({ width: 1500, height: 700 })

  await open()
  const reloaded = await measure(page)
  check(reloaded.widths.ID === 128 && reloaded.widths.Name === 360, 'the widths survive a reload')

  // Keyboard: the grip is a focusable splitter.
  const idGrip = page.getByRole('separator', { name: 'Resize column: ID' })
  await idGrip.focus()
  await page.keyboard.press('ArrowLeft')
  await page.waitForTimeout(100)
  check((await measure(page)).widths.ID === 112, 'ArrowLeft narrows the focused column by one 16px step')
  await page.locator('thead').first().screenshot({ path: `${OUT}/${PREFIX}-5-grip-focused.png` })

  await idGrip.dblclick()
  await page.getByRole('separator', { name: 'Resize column: Name' }).focus()
  await page.keyboard.press('Enter')
  await page.waitForTimeout(150)
  const reset = await measure(page)
  console.log('reset   ', JSON.stringify(reset))
  check(reset.widths.ID === 68 && reset.widths.Name === 160, 'double-click and Enter return a column to its default')
  check(reset.table === before.table, 'the table is back to its original width')

  // Every resizable column dragged to its FLOOR at once. A fixed layout does
  // not shrink content to fit -- it overlaps the next cell -- so a column whose
  // body cell does not clip draws its content over its neighbour at the floor.
  // Measured per body cell against its own column box rather than judged by eye:
  // the Status badge is the case that fails, and it fails by tens of pixels,
  // which reads as a rendering quirk rather than as the contract breaking.
  {
    const labels = await page.evaluate(() => [...document.querySelectorAll('thead th')]
      .map((th) => (th.querySelector('button, span')?.textContent || th.textContent || '').trim()))
    for (const label of labels) {
      if (!label) continue
      const grip = page.getByRole('separator', { name: `Resize column: ${label}` })
      if (await grip.count()) await dragGrip(page, label, -900)
    }
    // What matters is whether the cell CLIPS, not whether a child's layout box
    // is wider than it: under `overflow: hidden` the child still measures wide
    // but paints inside the cell, so a bounding-box comparison flags every
    // correctly-clipping column. The contract is per cell: content wider than
    // the box must not be painted outside it.
    const overflow = await page.evaluate(() => {
      const out = []
      for (const tr of document.querySelectorAll('tbody tr')) {
        for (const [i, td] of [...tr.children].entries()) {
          if (td.hasAttribute('colspan')) continue
          // 1px for sub-pixel layout rounding.
          const wider = td.scrollWidth > td.clientWidth + 1
          if (wider && getComputedStyle(td).overflowX === 'visible') {
            out.push(`col ${i} "${(td.textContent || '').trim().slice(0, 14)}" `
              + `overflows ${td.scrollWidth - td.clientWidth}px unclipped`)
          }
        }
      }
      return [...new Set(out)]
    })
    check(overflow.length === 0,
      `every column that outgrows its box at the floor clips instead of overlapping`
      + ` (${overflow.slice(0, 3).join('; ') || 'all clip'})`)
    await page.screenshot({ path: `${OUT}/${PREFIX}-8-all-columns-at-floor.png` })
  }

  // Light theme: stills only. The arithmetic above is theme-independent; what
  // a second theme can get wrong is the grip's colours, which only a picture
  // shows.
  {
    const light = await newSchedulePage(browser, { theme: 'light' })
    await openOn(light.page)
    await light.page.screenshot({ path: `${OUT}/${PREFIX}-6-default-light.png` })
    await dragGrip(light.page, 'ID', 60)
    await dragGrip(light.page, 'Name', 200)
    await light.page.getByRole('columnheader', { name: 'Type', exact: true }).hover()
    await light.page.waitForTimeout(250)
    await light.page.screenshot({ path: `${OUT}/${PREFIX}-7-widened-light.png` })
    await light.context.close()
  }

  // The recording: the change IS an interaction, and a still cannot show that
  // the boundary follows the pointer or that the neighbours hold still.
  {
    const videoDir = join(OUT, '.video-tmp')
    const rec = await newSchedulePage(browser, { theme: 'dark', videoDir })
    await rec.page.addInitScript(FAKE_CURSOR)
    await openOn(rec.page)
    await rec.page.waitForTimeout(600)
    await slowDrag(rec.page, 'ID', 60)
    await slowDrag(rec.page, 'Name', 200)
    await slowDrag(rec.page, 'Schedule', -70)
    // Double-click returns one column to its default.
    const nameGrip = await rec.page.getByRole('separator', { name: 'Resize column: Name' }).boundingBox()
    await rec.page.mouse.move(nameGrip.x + 3, nameGrip.y + nameGrip.height / 2, { steps: 25 })
    await rec.page.waitForTimeout(400)
    await rec.page.mouse.dblclick(nameGrip.x + 3, nameGrip.y + nameGrip.height / 2)
    await rec.page.waitForTimeout(1200)
    const video = rec.page.video()
    await rec.context.close()
    renameSync(await video.path(), `${OUT}/${PREFIX}-demo.webm`)
    rmSync(videoDir, { recursive: true, force: true })
  }

  await browser.close()
  srv.close()
  console.log(`\nwrote ${OUT}/${PREFIX}-*.png and ${OUT}/${PREFIX}-demo.webm`)
  if (failures.length) { console.error(`\n${failures.length} check(s) failed`); process.exit(1) }
}

main().catch(err => { console.error(err); process.exit(1) })
