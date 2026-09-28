import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, openMembersDm, podInfo } from './lib/crew-pod-harness.mjs'

// Real-pod capture for chat-core P5-e (#10005 PR-2): the non-page hosts mount
// the virtualized transcript, so a long thread costs the DOM of its viewport.
//   01 — Crew Members DM over a 200-row thread: hydrated rows vs mounted rows.
//   02 — the side panel (Activity → Side) on the shared virtualized scroller.
const OUT = process.argv[2]
const MEMBER = process.env.MEMBER || 'default'
if (!OUT) throw new Error('usage: <outDir>')
mkdirSync(OUT, { recursive: true })
const { BASE, authed } = podInfo(readFileSync)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2, timezoneId: 'UTC', locale: 'en' })
const page = await context.newPage()
await openMembersDm(page, authed, BASE, MEMBER)

const scroller = page.locator('#main-content .chat-container').first()
await scroller.waitFor({ state: 'visible', timeout: 20000 })
await page.waitForFunction(() => document.querySelectorAll('#main-content .chat-container [data-display-index]').length > 0, null, { timeout: 20000 })
await page.waitForTimeout(1200)
const dm = await page.evaluate(() => {
  const el = document.querySelector('#main-content .chat-container')
  const rows = [...el.querySelectorAll('[data-display-index]')].map((r) => Number(r.getAttribute('data-display-index')))
  const spacers = [...el.querySelectorAll('.vc-spacer-skeleton')].map((s) => s.getBoundingClientRect().height)
  return { mounted: rows.length, first: rows[0], last: rows[rows.length - 1], spacers, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
})
check('DM mounts a window, not the page (mounted rows < 50 hydrated rows)', dm.mounted > 0 && dm.mounted < 50, JSON.stringify(dm))
check('the unmounted rows are represented by a spacer with real height', dm.spacers.some((h) => h > 0), `spacers=${dm.spacers.map(Math.round)}`)
const f1 = join(OUT, '01-members-dm-virtualized.png')
await page.screenshot({ path: f1 })
console.log('wrote', f1)

// Side panel: the chat page's Activity viewer, Side tab.
await page.goto(authed('/'), { waitUntil: 'domcontentloaded' })
await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
await page.waitForTimeout(1000)
const activityBtn = page.getByRole('button', { name: 'Open activity panel' }).first()
if (await activityBtn.waitFor({ state: 'visible', timeout: 8000 }).then(() => true, () => false)) await activityBtn.click()
const tabMenu = page.getByRole('button', { name: 'Open side panel tab' }).first()
await tabMenu.waitFor({ state: 'visible', timeout: 15000 })
await tabMenu.click()
await page.getByRole('menuitem', { name: 'Side Chat' }).or(page.getByText('Side Chat', { exact: true })).first().click()
// The page's own scroller plus the side panel's: two virtualized transcripts.
await page.waitForFunction(() => document.querySelectorAll('.chat-container').length >= 2, null, { timeout: 15000 })
await page.waitForTimeout(800)
const sideScroller = page.locator('.chat-container').last()
const side = await sideScroller.evaluate((el) => ({ spacers: el.querySelectorAll('.vc-spacer-skeleton').length, sentinel: !!el.querySelector('[aria-hidden][style*="height: 1px"]') }))
check('side panel transcript is the shared virtualized scroller (two spacers)', side.spacers === 2, JSON.stringify(side))
const f2 = join(OUT, '02-side-chat-virtualized.png')
await page.screenshot({ path: f2 })
console.log('wrote', f2)
// App embed: the spec-builder column over a 300-row transcript. The poll is
// bounded to one page; the bar widens it by a page per press.
const SPEC = process.env.SPEC || 'p5e-demo'
let embed = null
if (SPEC !== 'skip') {
  await page.goto(authed(`/apps/spec-builder`), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const specRow = page.getByRole('button', { name: new RegExp(SPEC) }).or(page.getByText(SPEC, { exact: true })).first()
  await specRow.waitFor({ state: 'visible', timeout: 20000 })
  await specRow.click()
  const bar = page.getByTestId('load-earlier-messages')
  await bar.waitFor({ state: 'visible', timeout: 30000 })
  const embedScroller = page.locator('.chat-container').last()
  await embedScroller.evaluate((el) => { el.scrollTop = 0 })
  await page.waitForTimeout(600)
  const before = await embedScroller.evaluate((el) => el.querySelectorAll('[data-display-index]').length)
  check('embed mounts a window of the 200-row page, with the earlier bar', before > 0 && before < 200, `rows=${before}`)
  const f3 = join(OUT, '03-spec-builder-embed-load-earlier.png')
  await page.screenshot({ path: f3 })
  console.log('wrote', f3)
  await bar.click()
  await page.waitForFunction(() => document.querySelectorAll('.vc-spacer-skeleton').length >= 2, null, { timeout: 30000 })
  await page.waitForTimeout(2500)
  const afterTotal = await page.evaluate(() => {
    const el = [...document.querySelectorAll('.chat-container')].pop()
    const top = el.querySelector('.vc-spacer-skeleton')
    return { spacerTop: top ? top.getBoundingClientRect().height : 0, scrollHeight: el.scrollHeight }
  })
  embed = { before, afterTotal }
}
console.log(JSON.stringify({ dm, side, embed }))
await context.close()
await browser.close()
