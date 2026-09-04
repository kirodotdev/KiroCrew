/**
 * Screenshot harness for the Steering tab's linked (symlinked) entries: the
 * Linked chip on a row and in the detail header, disabled Edit/Delete on a
 * read-only entry, and the enabled controls on an ordinary neighbour as the
 * control frame.
 *
 * Same shape as capture-skills-name-preview.mjs: serves the REAL built SPA
 * (website/dist) behind a loopback server and answers /api/** from the shared
 * fixture router. Nothing here talks to a gateway.
 *
 * Three frames:
 *
 *   01-linked-selected     the symlinked document auto-selected: Linked chip on
 *                          the row AND the detail header, Edit + Delete disabled.
 *   02-linked-tooltip      same page with the chip's title text asserted via the
 *                          DOM (tooltips do not render into screenshots), shown
 *                          as a caption overlay so the reviewer sees the copy.
 *   03-regular-control     the ordinary entry selected: no chip, Edit + Delete
 *                          enabled — proof the disable is scoped to linked rows.
 *
 * Rebuild the SPA (`npm run build`) before running: serve-dist serves whatever
 * is on disk, so shooting a UI change against a stale dist yields an "after"
 * frame identical to before — indistinguishable from the change not working.
 *
 * Usage: node scripts/capture-steering-linked.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/steering-linked-readonly'
const PREFIX = process.argv[3] || 'shot'

mkdirSync(OUT, { recursive: true })

const LINKED_TARGET = '~/dotfiles/team-conventions.md'

const LISTING = {
  files: [
    {
      key: 'user/team-conventions.md', name: 'team-conventions.md',
      rel: 'team-conventions.md', source: 'user',
      path: '~/.kiro/steering/team-conventions.md', size: 64,
      description: 'Team conventions', inclusion: 'always',
      inclusion_declared: '', file_match_pattern: '',
      linked: true, editable: false, target: LINKED_TARGET,
    },
    {
      key: 'user/personal.md', name: 'personal.md', rel: 'personal.md',
      source: 'user', path: '~/.kiro/steering/personal.md', size: 40,
      description: 'Personal rules', inclusion: 'always',
      inclusion_declared: '', file_match_pattern: '',
      linked: false, editable: true, target: '',
    },
  ],
  roots: [{ source: 'user', path: '~/.kiro/steering', exists: true }],
  project: '',
  project_key: '',
  project_state: 'none',
}

const DETAIL = {
  'user/team-conventions.md': {
    key: 'user/team-conventions.md',
    content: '# Team conventions\n\nShared rules symlinked from a dotfiles checkout.\n',
    path: LINKED_TARGET, source: 'user',
  },
  'user/personal.md': {
    key: 'user/personal.md',
    content: '# Personal rules\n\nOrdinary editable steering document.\n',
    path: '~/.kiro/steering/personal.md', source: 'user',
  },
}

const { srv, base: BASE } = await serveDist()
const browser = await chromium.launch()

const shoot = (page, name) =>
  page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, animations: 'disabled' })

try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/steering') { await json(route, LISTING); return true }
      const m = path.match(/^\/api\/steering\/(.+)$/)
      if (m) {
        const detail = DETAIL[decodeURIComponent(m[1])]
        if (detail) { await json(route, detail); return true }
        await json(route, { error: 'not found' }, 404)
        return true
      }
      return false
    },
  })

  await page.goto(`${BASE}/capabilities?tab=steering`, { waitUntil: 'domcontentloaded' })

  // The linked entry sorts first, so it is the auto-selected one. Wait for its
  // detail body, the row's icon-only linked marker, the visible read-only
  // banner, and the DISABLED state before shooting — an unasserted frame is
  // the one that goes blank without the run failing.
  await page.locator('text=Shared rules symlinked from').first().waitFor({ timeout: 20000 })
  await page.getByTestId('steering-linked-chip').first().waitFor()
  await page.locator(`text=Linked from ${LINKED_TARGET}`).first().waitFor()
  const editBtn = page.getByRole('button', { name: 'Edit', exact: true })
  const deleteBtn = page.getByRole('button', { name: 'Delete', exact: true })
  if (await editBtn.isEnabled()) throw new Error('Edit is enabled on a linked entry')
  if (await deleteBtn.isEnabled()) throw new Error('Delete is enabled on a linked entry')
  await shoot(page, '01-linked-selected')

  // Tooltips never render into a screenshot, so surface the title copy as an
  // in-page caption for the frame — after asserting it is the real attribute.
  const title = await editBtn.getAttribute('title')
  if (!title || !title.includes(LINKED_TARGET)) {
    throw new Error(`Edit title does not name the target: ${title}`)
  }
  await page.evaluate(text => {
    const el = document.createElement('div')
    el.textContent = `title: ${text}`
    el.setAttribute('style',
      'position:fixed;bottom:16px;left:50%;transform:translateX(-50%);'
      + 'background:#111;color:#fff;padding:8px 14px;border-radius:8px;'
      + 'font:13px monospace;z-index:9999;box-shadow:0 2px 12px rgba(0,0,0,.5)')
    document.body.appendChild(el)
  }, title)
  await shoot(page, '02-linked-tooltip')
  await page.evaluate(() => document.body.lastElementChild.remove())

  // Control frame: the ordinary neighbour keeps live controls and no chip.
  await page.getByRole('button', { name: 'Select personal.md' }).click()
  await page.locator('text=Ordinary editable steering document').first().waitFor()
  await editBtn.waitFor()
  if (!(await editBtn.isEnabled())) throw new Error('Edit is disabled on a regular entry')
  if (!(await deleteBtn.isEnabled())) throw new Error('Delete is disabled on a regular entry')
  await shoot(page, '03-regular-control')

  console.log(`wrote 3 frames to ${OUT}`)
} finally {
  await browser.close()
  srv.close()
}
