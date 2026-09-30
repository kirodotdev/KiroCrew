/**
 * Screenshot harness for the Session Color removal:
 *   1. create — the create form no longer holds a colour picker
 *   2. edit   — the Triggers pane is Triggers alone
 *
 * Gateway-free, the same pattern as the other capture harnesses in this folder:
 * the REAL built SPA (website/dist) behind the shared in-process static server,
 * with every /api/** call answered from fixtures via Playwright route
 * interception. The dashboard API is token-gated and minting a token is refused
 * for an agent, so a live gateway would only ever screenshot the error state.
 *
 * SELF-CHECKS throw rather than write a stale frame: NO session-colour control
 * may survive in either modal.
 *
 * Usage: node scripts/capture-session-color-removal.mjs [outDir] [prefix]
 */
import { mkdirSync } from 'node:fs'
import { openAgentsPage, openCreateModal, openEditPane } from './lib/agents-page-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-color-removal'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

/** One crew carrying a stored session_color, so the frames also prove the
 *  removal does not depend on the value being empty. */
const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '#6366f1' },
]

/** The colour picker's own controls, by the accessible names it used to expose.
 *  Asserted absent rather than eyeballed — a frame cannot prove a negative. */
const COLOR_CONTROLS = [/session colour/i, /session color/i, /#rrggbb/i]

async function assertNoColorControl(scope, where) {
  for (const pattern of COLOR_CONTROLS) {
    if (await scope.getByLabel(pattern).count()) {
      throw new Error(`${where}: a session-colour control is still mounted (${pattern})`)
    }
  }
  if (await scope.getByPlaceholder('#rrggbb').count()) {
    throw new Error(`${where}: the session-colour hex input is still mounted`)
  }
}

const { srv, browser, page } = await openAgentsPage({ crews: CREWS })

try {
  /* ── 1. Create ─────────────────────────────────────────────────────────── */
  const create = await openCreateModal(page)

  await assertNoColorControl(create, 'create modal')

  const out1 = `${OUT}/${PREFIX}-create.png`
  await create.screenshot({ path: out1 })
  console.log(`wrote ${out1}`)
  await page.keyboard.press('Escape')

  /* ── 2. Edit → Triggers pane ───────────────────────────────────────────── */
  const edit = await openEditPane(page, 'default', /triggers/i)

  // The pane must still own Triggers — a frame that lost both controls would
  // pass a bare "no colour picker" check while proving the wrong thing.
  if (!(await edit.getByLabel(/triggers/i).count())) {
    throw new Error('edit modal: the Triggers pane no longer renders its own field')
  }
  await assertNoColorControl(edit, 'edit modal Triggers pane')

  const out2 = `${OUT}/${PREFIX}-edit-triggers.png`
  await edit.screenshot({ path: out2 })
  console.log(`wrote ${out2}`)
} finally {
  await browser.close()
  srv.close()
}
