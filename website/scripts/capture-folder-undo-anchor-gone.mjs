/**
 * Capture + regression harness for the folder Undo fallback status line
 * (`folder-undo-anchor-gone`, PR #15041).
 *
 * A folder dragged into another folder arms an Undo. The Undo PATCH names the
 * folder's old neighbours as `before`/`after` anchors; when one of them has moved
 * or been deleted, the gateway answers 409 `folder_anchor_not_sibling`, the client
 * retries with `parent_id` alone, and the sidebar says where the folder went in a
 * status line above the tree. This harness drives that path in the REAL built SPA
 * (website/dist) with real pointer events, with /api/** answered by the shared
 * stub: the first anchored Undo PATCH gets the 409, the parent-only retry lands.
 *
 * Scenarios, each in dark and light:
 *   - nested: a folder with a long name is dragged out of "Archive" and undone,
 *     so the line reads "went back under Archive" and shows how a long name wraps
 *     at sidebar width;
 *   - top: a root folder is dragged into "Later" and undone, so the line reads
 *     "went back to the top level".
 *
 * It asserts as well as photographs, exiting non-zero if any check fails: the
 * line renders as role=status, names the folder and the parent, is not under the
 * "Folder update failed" error surface, and the folder row sits under the parent
 * the line names.
 *
 * Usage: node scripts/capture-folder-undo-anchor-gone.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/15041-folder-undo-anchor-gone'
const VIEW = { width: 1400, height: 900 }
const ARCHIVE = 'farchive'
const LATER = 'flater'
const PREV = 'fprev'
const CHILD = 'fchild'
const NEXT = 'fnext'
const INBOX = 'finbox'
const LONG_NAME = 'Quarterly planning notes and customer follow-ups for the platform team'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/** A fresh fixture per scenario: the write handlers mutate it in place. */
function fixture() {
  return [
    { id: ARCHIVE, name: 'Archive', order: 0, rank: 'F' },
    { id: LATER, name: 'Later', order: 1, rank: 'V' },
    { id: INBOX, name: 'Inbox', order: 2, rank: 'X' },
    { id: PREV, name: 'Drafts', order: 0, rank: 'F', parent_id: ARCHIVE },
    { id: CHILD, name: LONG_NAME, order: 1, rank: 'N', parent_id: ARCHIVE },
    { id: NEXT, name: 'Receipts', order: 2, rank: 'V', parent_id: ARCHIVE },
  ]
}

const mkSlot = (key, title, folderId) => ({
  key, title, running: false, last_message: '', messages: 3, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: folderId, modified: now,
  tags: [], source_links: [], source_links_total: 0,
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` -- ${note}` : ''}`)
  }

  async function run(theme, variant) {
    const folders = fixture()
    const slots = [
      mkSlot('chat-c1', 'Planning thread', CHILD),
      mkSlot('chat-l1', 'Sprint planning', LATER),
    ]
    const calls = []
    const extra = async (path, route) => {
      const method = route.request().method()
      if (path.startsWith('/api/chat/folders/') && method === 'PATCH') {
        const id = decodeURIComponent(path.slice('/api/chat/folders/'.length))
        const body = route.request().postDataJSON?.() ?? {}
        calls.push({ id, body })
        const f = folders.find(x => x.id === id)
        // The Undo's anchored PATCH: its captured neighbour "has moved", so the
        // gateway refuses the anchor exactly as the real one does.
        if (('before' in body || 'after' in body) && calls.length > 1) {
          await json(route, { error: 'The folder it sat next to has moved or was deleted.', code: 'folder_anchor_not_sibling' }, 409)
          return true
        }
        if (f && 'parent_id' in body) {
          f.parent_id = body.parent_id || undefined
          // The gateway seats a parent-only move last in its new section.
          f.rank = 'Z'
        }
        await json(route, f ?? { ok: true })
        return true
      }
      if (path === '/api/chat/tags') { await json(route, []); return true }
      return false
    }

    const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders, slots, theme, extra })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`[data-folder-row="${CHILD}"]`, { timeout: 15000 })
    await page.waitForTimeout(500)

    const header = (id) => page.locator(`[data-folder-row="${id}"]`).first()
    async function drag(fromId, toId, bandFrac) {
      const from = await header(fromId).boundingBox()
      const to = await header(toId).boundingBox()
      if (!from || !to) throw new Error(`header not found: ${fromId} -> ${toId}`)
      const sx = from.x + from.width / 2
      const sy = from.y + from.height / 2
      const tx = to.x + to.width / 2
      const ty = to.y + to.height * bandFrac
      await page.mouse.move(sx, sy)
      await page.mouse.down()
      await page.mouse.move(sx + 8, sy + 4, { steps: 4 })
      await page.waitForTimeout(150)
      for (let i = 1; i <= 12; i++) {
        await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
        await page.waitForTimeout(35)
      }
      await page.waitForTimeout(400)
      await page.mouse.up()
      await page.waitForTimeout(800)
    }

    const moved = variant === 'nested' ? CHILD : INBOX
    const parentName = variant === 'nested' ? 'Archive' : null
    // nested: drag the long-named child out of Archive into Later.
    // top: drag the root folder Inbox into Later.
    if (variant === 'nested') await drag(CHILD, LATER, 0.5)
    else await drag(INBOX, LATER, 0.5)

    const tag = `${variant}-${theme}`
    const undo = page.getByTestId('session-move-undo-button')
    const armed = await undo.count() > 0
    record(`${tag}: the drag armed an Undo`, armed, `patches=${JSON.stringify(calls)}`)
    if (!armed) {
      await page.screenshot({ path: `${OUT}/${tag}-no-undo.png` })
      await context.close()
      return
    }
    await undo.first().click()
    const notice = page.getByTestId('folder-undo-anchor-gone')
    await notice.waitFor({ timeout: 8000 }).catch(() => {})
    const shown = await notice.count() > 0
    record(`${tag}: the status line renders`, shown)
    if (shown) {
      const text = (await notice.textContent()) ?? ''
      const expectWhere = parentName ? `went back under ${parentName}` : 'went back to the top level'
      record(`${tag}: it names the folder and where it went`,
        text.includes(folders.find(f => f.id === moved).name) && text.includes(expectWhere), text)
      record(`${tag}: it is a status line`, (await notice.getAttribute('role')) === 'status')
      record(`${tag}: no "Folder update failed" error is shown`,
        (await page.getByTestId('folder-action-error').count()) === 0)
      const f = folders.find(x => x.id === moved)
      record(`${tag}: the folder is back under the parent the line names`,
        (f.parent_id ?? null) === (variant === 'nested' ? ARCHIVE : null), `parent_id=${f.parent_id}`)
      const anchored = calls.find((c, i) => i > 0 && ('before' in c.body || 'after' in c.body))
      record(`${tag}: the Undo sent an anchored PATCH first`, !!anchored, JSON.stringify(calls))
    }
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${OUT}/${tag}.png` })
    console.log('wrote', `${OUT}/${tag}.png`)
    await context.close()
  }

  for (const theme of ['dark', 'light']) {
    await run(theme, 'nested')
    await run(theme, 'top')
  }

  await browser.close()
  srv.close()
  const failed = results.filter(r => !r.pass)
  console.log(`\n--- ${results.length - failed.length}/${results.length} assertions passed ---`)
  if (failed.length) {
    for (const f of failed) console.log(`FAILED: ${f.name} -- ${f.note}`)
    process.exitCode = 1
  }
}

main().catch(e => {
  console.error(e)
  process.exitCode = 1
  setTimeout(() => process.exit(1), 500)
})
