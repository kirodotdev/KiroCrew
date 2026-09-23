/**
 * Screenshots of DagView's two approval states, driven through the isolated
 * capture entry (website/capture/dag-approval-labels.html), which mounts the
 * REAL DagView with an in_progress task parked at its approval gate and a
 * later task whose gate has not been reached.
 *
 * Each frame ASSERTS the state before writing the file, so a frame cannot
 * silently document the wrong build:
 *   after (default): the in_progress node with the approvalMap entry carries the
 *     Approve / Deny buttons, the warn halo AND the "Needs Approval" word, and
 *     the halo encloses the buttons; the pending gated node reads "Approval gate
 *     ahead", has no halo and no buttons; the legend names both states.
 *   --before: the pre-change DagView. The buttons and halo sit on the in_progress
 *     node, but that node reads "in progress" while the pending gated node is the
 *     one that says "needs approval".
 * Both modes also assert that no dialog is open over the frame.
 *
 * The capture entry is part of this change, so a checkout of the OLD DagView
 * only has it if you put it there: copy `capture/dag-approval-labels.{html,tsx}`
 * and this script into the old checkout's `website/` before serving it. The
 * script checks that the entry is actually served and stops with that
 * instruction instead of timing out on a 404.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6843 --strictPort   # in another shell
 *   node scripts/capture-dag-approval-labels.mjs http://127.0.0.1:6843 ../temp-screenshots/dag-approval-labels [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6843'
const OUT = process.argv[3] || '../temp-screenshots/dag-approval-labels'
const BEFORE = process.argv.includes('--before')
const ENTRY = '/capture/dag-approval-labels.html'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 560, height: 640 }, deviceScaleFactor: 2 })

let failed = false
for (const theme of ['dark', 'light']) {
  const response = await page.goto(`${BASE}${ENTRY}?theme=${theme}`)
  if (!response || !response.ok()) {
    console.error(`${ENTRY} answered ${response ? response.status() : 'nothing'} at ${BASE}. The served checkout has no capture entry: copy capture/dag-approval-labels.{html,tsx} and scripts/capture-dag-approval-labels.mjs into its website/ directory and serve it again.`)
    failed = true
    break
  }
  await page.waitForSelector('[data-capture-section="dag-view"] svg rect')
  // Halo pulse and edge dots are animated; hold them so the frame is deterministic.
  await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })

  const dialogs = await page.getByRole('dialog').count()
  const cards = page.locator('[data-capture-section="dag-view"] svg g.cursor-pointer')
  const card = title => cards.filter({ hasText: title })
  const word = async title => (await card(title).locator('text').first().textContent()) || ''
  const buttons = async title => card(title).locator('foreignObject button').count()
  // The halo is the only warn-stroked rect drawn at 3px (node borders are 2 / 2.5).
  const haloOf = title => card(title).locator('rect[stroke="var(--warn)"][stroke-width="3"]')
  // Does the halo's box contain the button row's box?
  const haloWrapsButtons = async title => {
    const halo = await haloOf(title).boundingBox()
    const row = await card(title).locator('foreignObject').filter({ has: page.locator('button') }).boundingBox()
    return !!halo && !!row && halo.y <= row.y && halo.y + halo.height >= row.y + row.height
  }
  // DagView's root holds the scroll box and, after it, the legend row.
  const legend = (await page.locator('[data-capture-section="dag-view"] > div > div').last().innerText()).replace(/\s+/g, ' ')

  const state = {
    implement: { word: await word('Implement'), buttons: await buttons('Implement'), halo: await haloOf('Implement').count(), wraps: await haloWrapsButtons('Implement') },
    deploy: { word: await word('Deploy'), buttons: await buttons('Deploy'), halo: await haloOf('Deploy').count() },
    plan: await word('Plan'),
    announce: await word('Announce'),
  }
  const ok = dialogs === 0 && (await cards.count()) === 4 &&
    state.implement.buttons === 2 && state.implement.halo === 1 &&
    state.deploy.buttons === 0 && state.deploy.halo === 0 &&
    (BEFORE
      ? state.implement.word === 'in progress' && state.deploy.word === 'needs approval' && !/Approval gate/.test(legend)
      : state.implement.word === 'Needs Approval' && state.implement.wraps && state.deploy.word === 'Approval gate ahead' &&
        state.plan === 'Done' && state.announce === 'Pending' && /Needs Approval/.test(legend) && /Approval gate ahead/.test(legend))
  console.log(`${theme}${BEFORE ? ' (before)' : ''}: dialogs=${dialogs} ${JSON.stringify(state)} legend="${legend}" ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/dag-approval-labels-${theme}${BEFORE ? '-before' : ''}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
