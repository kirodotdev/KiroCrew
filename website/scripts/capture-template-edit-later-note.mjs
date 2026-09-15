/**
 * Screenshot harness for the template "edit later" note:
 *   1. create — the Agent Template field carries the note
 *   2. edit   — the template pane renders the SAME field with NO note
 *
 * Gateway-free, the same pattern as the other capture harnesses in this folder:
 * the REAL built SPA (website/dist) behind the shared in-process static server,
 * with every /api/** call answered from fixtures via Playwright route
 * interception. The dashboard API is token-gated and minting a token is refused
 * for an agent, so a live gateway would only ever screenshot the error state.
 *
 * SELF-CHECKS throw rather than write a stale frame: the note must be present
 * in create and absent in the edit template pane — the same split the
 * templateEditLaterNote vitest suite pins.
 *
 * Usage: node scripts/capture-template-edit-later-note.mjs [outDir] [prefix]
 */
import { mkdirSync } from 'node:fs'
import { openAgentsPage, openCreateModal, openEditPane } from './lib/agents-page-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/template-edit-later-note'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '' },
]

const NOTE = /you can switch this agent's template anytime/i

const { srv, browser, page } = await openAgentsPage({ crews: CREWS })

try {
  /* ── 1. Create — note present ──────────────────────────────────────────── */
  const create = await openCreateModal(page)

  if (!(await create.getByText(NOTE).count())) {
    throw new Error('create modal never rendered the edit-later note')
  }

  const out1 = `${OUT}/${PREFIX}-create.png`
  await create.screenshot({ path: out1 })
  console.log(`wrote ${out1}`)
  await page.keyboard.press('Escape')

  /* ── 2. Edit → template pane — note absent ─────────────────────────────── */
  const edit = await openEditPane(page, 'default', /template/i)

  // The pane must still own the template select — a frame that lost the field
  // would pass a bare "no note" check while proving the wrong thing.
  if (!(await edit.getByRole('combobox', { name: /agent template/i }).count())) {
    throw new Error('edit modal: the template pane no longer renders its own field')
  }
  if (await edit.getByText(NOTE).count()) {
    throw new Error('edit modal: the create-only note leaked into the template pane')
  }

  const out2 = `${OUT}/${PREFIX}-edit-template.png`
  await edit.screenshot({ path: out2 })
  console.log(`wrote ${out2}`)
} finally {
  await browser.close()
  srv.close()
}
