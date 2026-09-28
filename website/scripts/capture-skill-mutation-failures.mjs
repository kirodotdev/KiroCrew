/**
 * Screenshot harness for the Skills tab's update/delete failure notices.
 *
 * Before this change a failed save showed nothing at all, and a failed delete
 * silently re-rendered the row after its optimistic-removal rollback. Two frames
 * prove the fix:
 *
 *   1. `update-failure.png` — the detail editor is open, the PUT failed, and an
 *      inline ErrorNotice under the Save/Cancel row names the failure. No
 *      Ask-agent hand-off here: the form still holds the unsaved edit.
 *   2. `delete-failure.png` — the DELETE failed, the rolled-back row is visible
 *      in the list again, and a block ErrorNotice above the list says why, with
 *      the Ask-agent hand-off.
 *
 * The frames cannot lie: the script ASSERTS the update notice is present in
 * frame 1, and that frame 2 shows BOTH the delete notice and the restored row —
 * a fix that surfaced the error by breaking the rollback fails the run.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through `stubDashboardApi`.
 *
 * Usage: node scripts/capture-skill-mutation-failures.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/skill-mutation-failure-shots'

mkdirSync(OUT, { recursive: true })

const SKILL = {
  key: 'my-notes',
  name: 'my-notes',
  description: 'Personal note-taking conventions',
  source: 'kirocrew',
  loaded_by_agents: [],
}
// A survivor: frame 4 deletes my-notes while its DELETE hangs, and the
// optimistic removal must leave a row to auto-select (and edit).
const OTHER = {
  key: 'meeting-prep',
  name: 'meeting-prep',
  description: 'How to prep the weekly sync',
  source: 'kirocrew',
  loaded_by_agents: [],
}
const SKILL_MD = '---\nname: my-notes\ndescription: Personal note-taking conventions\n---\nUse dated headers.\n'

const { srv, base } = await serveDist()
const browser = await chromium.launch()

// One-shot flag for the skills list GET; see the stub below.
let skillsListServed = false
// 'fail' answers the PUT with a 500; 'hang' leaves it pending so the
// in-flight gated states (frame 3) can be photographed.
let putMode = 'fail'
// 'fail' answers the DELETE with a 500 immediately; 'hold' stashes the route
// so frame 4 can deliver the failure only after the editor is open.
let deleteMode = 'fail'
let heldDeleteRoute = null

/** Shared /api/skills/** fixtures — used by the desktop and mobile contexts. */
const apiExtra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/skills' && method === 'GET') {
    // Serve the list ONCE and leave every later GET pending: deleteSkill's
    // onSettled invalidates ['skills'], and a stub that keeps answering
    // would refetch the row back into the list even with the rollback
    // broken — the restored-row assertion below would then be unfailable.
    // Hanging the refetch makes row visibility the optimistic write and
    // its rollback's alone (same shape the unit tests use).
    if (skillsListServed) return true
    skillsListServed = true
    await json(route, [SKILL, OTHER])
    return true
  }
  if (path === '/api/skills/meeting-prep/-/tree') {
    await json(route, { entries: [{ path: 'SKILL.md', type: 'file', size: 40 }] })
    return true
  }
  if (path.startsWith('/api/skills/meeting-prep/-/file')) {
    await json(route, { content: '---\nname: meeting-prep\n---\nAgenda first.\n' })
    return true
  }
  if (path === '/api/skills/meeting-prep' && method === 'GET') {
    await json(route, { name: 'meeting-prep', content: '---\nname: meeting-prep\n---\nAgenda first.\n' })
    return true
  }
  if (path === '/api/skills/-/pending') {
    await json(route, { pending: [] })
    return true
  }
  if (path === '/api/skills/-/trust') {
    await json(route, { grants: [] })
    return true
  }
  if (path === '/api/skills/-/budget') {
    await json(route, { skills: [], total: 0 })
    return true
  }
  if (path === '/api/skills/my-notes/-/tree') {
    await json(route, { entries: [{ path: 'SKILL.md', type: 'file', size: SKILL_MD.length }] })
    return true
  }
  if (path.startsWith('/api/skills/my-notes/-/file')) {
    await json(route, { content: SKILL_MD })
    return true
  }
  if (path === '/api/skills/my-notes' && method === 'GET') {
    await json(route, { name: 'my-notes', content: SKILL_MD })
    return true
  }
  const isSkillDoc = path === '/api/skills/my-notes' || path === '/api/skills/meeting-prep'
  if (isSkillDoc && method === 'PUT') {
    if (putMode === 'hang') return true  // leave pending: the in-flight frame
    await json(route, { error: 'disk full: no space left on device' }, 500)
    return true
  }
  if (isSkillDoc && method === 'DELETE') {
    if (deleteMode === 'hold') { heldDeleteRoute = route; return true }
    await json(route, { error: 'permission denied' }, 500)
    return true
  }
  return false
}

try {
  const context = await browser.newContext({
    // 1x, per the image-read ceiling the sibling harnesses observe.
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)
  // The Delete button confirms through window.confirm.
  page.on('dialog', d => { void d.accept() })

  await stubDashboardApi(page, {
    localStorageEntries: { 'mc-lang': 'en' },
    extra: apiExtra,
  })

  await page.goto(base + '/capabilities?tab=skills', { waitUntil: 'domcontentloaded' })

  // Frame 1 — the failed save, surfaced next to the editor. The bare name
  // "Edit" also matches a SettingRef chip's affordance elsewhere on the tab, so
  // the click is scoped to the detail header's action row via its Delete sibling.
  const actionRow = page.getByRole('button', { name: 'Delete' }).locator('..')
  await actionRow.getByRole('button', { name: 'Edit' }).click()
  await page.getByRole('button', { name: 'Save' }).waitFor({ timeout: 10000 })
  await page.getByRole('button', { name: 'Save' }).click()
  const updateNotice = page.getByTestId('skill-update-failure')
  await updateNotice.waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  // Read the copy now: Cancel below unmounts the editor and the notice with it.
  const updateText = await updateNotice.textContent()
  await page.screenshot({ path: join(OUT, 'update-failure.png') })

  // Frame 2 — the failed delete: notice above the list, row restored.
  await page.getByRole('button', { name: 'Cancel' }).click()
  await page.getByRole('button', { name: 'Delete' }).click()
  const deleteNotice = page.getByTestId('skill-delete-failure')
  await deleteNotice.waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  // The row probe is scoped to the LIST PANE and the row's own accessible
  // name: a bare text probe would substring-match the banner ("Could not
  // delete “My Notes”…"), making this assertion unfailable, and the rows are
  // role="button" (no option nodes exist).
  const rowVisible = await page.getByRole('listbox', { name: 'Skills' }).getByRole('button', { name: 'Select My Notes' }).count()
  await page.screenshot({ path: join(OUT, 'delete-failure.png') })

  const deleteText = await deleteNotice.textContent()

  // Frame 3 — the in-flight gated states: PUT hung, Save/Cancel disabled,
  // rows inert (aria-disabled, no pointer affordance).
  putMode = 'hang'
  const actionRow2 = page.getByRole('button', { name: 'Delete' }).locator('..')
  await actionRow2.getByRole('button', { name: 'Edit' }).click()
  await page.getByRole('button', { name: 'Save' }).waitFor({ timeout: 10000 })
  await page.getByRole('button', { name: 'Save' }).click()
  // While the PUT hangs the button announces itself: 'Saving…', disabled.
  await page.getByRole('button', { name: 'Saving…' }).and(page.locator('[disabled]')).waitFor({ timeout: 10000 })
  const cancelDisabled = await page.getByRole('button', { name: 'Cancel' }).isDisabled()
  const inertRows = await page.locator('[role="button"][aria-disabled="true"]').count()
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, 'save-in-flight.png') })

  // Frame 4 — the latched mobile list: a delete failure arriving while the
  // editor is open is retained; after Back it shows WITHOUT the hand-off,
  // because the hidden editor still holds the draft.
  const mobile = await browser.newContext({ viewport: { width: 480, height: 900 }, deviceScaleFactor: 1 })
  const mpage = await mobile.newPage()
  logPageProblems(mpage)
  mpage.on('dialog', d => { void d.accept() })
  skillsListServed = false
  putMode = 'fail'
  deleteMode = 'hold'
  await stubDashboardApi(mpage, {
    localStorageEntries: { 'mc-lang': 'en' },
    extra: apiExtra,
  })
  await mpage.goto(base + '/capabilities?tab=skills', { waitUntil: 'domcontentloaded' })
  await mpage.getByRole('button', { name: 'Select My Notes' }).click()
  // Start the delete; the route is HELD, so the request hangs.
  await mpage.getByRole('button', { name: 'Delete' }).click()
  // Open the editor while the delete is still in flight…
  const mEdit = mpage.getByRole('button', { name: 'Delete' }).locator('..').getByRole('button', { name: 'Edit' })
  await mEdit.click()
  await mpage.getByRole('button', { name: 'Save' }).waitFor({ timeout: 10000 })
  // …then deliver the failure: suppressed, the editor is visible.
  await json(heldDeleteRoute, { error: 'permission denied' }, 500)
  // Back latches the session; the retained banner shows on the list.
  await mpage.locator('button.min-h-11').filter({ hasText: 'Skills' }).click()
  const mBanner = mpage.getByTestId('skill-delete-failure')
  await mBanner.waitFor({ timeout: 10000 })
  const latchedHandoff = await mBanner.getByText('Ask the agent').count()
  await mpage.waitForTimeout(400)
  await mpage.screenshot({ path: join(OUT, 'latched-banner-no-handoff.png') })
  // A row tap on the latched list REOPENS the latched editor (draft
  // preservation), so to photograph the unlatched hand-off: dismiss the
  // banner, end the session through its own exit, then fail a fresh delete.
  deleteMode = 'fail'
  await mBanner.getByRole('button', { name: 'Dismiss' }).click()
  await mpage.getByRole('button', { name: 'Select My Notes' }).click()   // reopens the latched editor
  await mpage.getByRole('button', { name: 'Cancel' }).click()            // ends the session
  await mpage.getByRole('button', { name: 'Delete' }).click()
  await mBanner.waitFor({ timeout: 10000 })
  const unlatchedHandoff = await mBanner.getByText('Ask the agent').count()
  await mobile.close()

  console.log('frame 1 notice:', updateText)
  console.log('frame 2 notice:', deleteText, '| restored row elements:', rowVisible)
  console.log('frame 3 — Cancel disabled:', cancelDisabled, '| inert rows:', inertRows)
  console.log('frame 4 — hand-off unlatched/latched:', unlatchedHandoff, '/', latchedHandoff)

  if (!/Save failed/.test(updateText || '') || !/disk full/.test(updateText || '')) {
    console.error('FAIL: frame 1 does not carry the update-failure notice')
    process.exitCode = 1
  }
  if (!/Could not delete/.test(deleteText || '')) {
    console.error('FAIL: frame 2 does not carry the delete-failure notice')
    process.exitCode = 1
  }
  if (!rowVisible) {
    console.error('FAIL: the rolled-back row is not visible in frame 2')
    process.exitCode = 1
  }
  if (!cancelDisabled || !inertRows) {
    console.error('FAIL: frame 3 does not show the in-flight gated states')
    process.exitCode = 1
  }
  if (unlatchedHandoff !== 1 || latchedHandoff !== 0) {
    console.error('FAIL: frame 4 hand-off gating is wrong (want present unlatched, absent latched)')
    process.exitCode = 1
  }
  if (!process.exitCode) console.log('wrote', OUT)

  await context.close()
} finally {
  await browser.close()
  srv.close()
}
