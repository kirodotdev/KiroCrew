import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { editRuleViewer, mayEdit, type EditRuleViewer } from './editRule'
import type { CrewModeResponse } from './sharedCrewMode'

/** The table the gateway's `test_shared_crew_edit_rule.py` runs on the real identities. */
const FIXTURE = path.resolve(__dirname, '../../../test/fixtures/shared_crew_edit_rule.json')
type Case = { preview: boolean; editor: string; author: string; expect: boolean; why: string }
const cases: Case[] = JSON.parse(readFileSync(FIXTURE, 'utf8')).cases

const OWNER_IDS = ['p_owner_local', 'p_owner_startup', 'p_owner_tailnet']
/** Each table editor as the mode route describes it: `viewer_ids` and `edit_rule`. */
const EDITORS: Record<string, { ids: string[]; isOwner: boolean; relayTeammate: boolean }> = {
  owner_local: { ids: OWNER_IDS, isOwner: true, relayTeammate: false },
  owner_startup: { ids: OWNER_IDS, isOwner: true, relayTeammate: false },
  owner_tailnet: { ids: OWNER_IDS, isOwner: true, relayTeammate: false },
  owner_relay: { ids: OWNER_IDS, isOwner: true, relayTeammate: false },
  teammate_relay: { ids: ['p_teammate_relay'], isOwner: false, relayTeammate: true },
  teammate_tailnet: { ids: ['p_teammate_tailnet'], isOwner: false, relayTeammate: false },
  unknown: { ids: ['p_unknown'], isOwner: false, relayTeammate: false },
  app: { ids: ['p_app'], isOwner: false, relayTeammate: false },
}
/** Each table author as the gateway stamps it (`attribution.author_stamp`). */
const AUTHORS: Record<string, Record<string, unknown>> = {
  absent: {},
  damaged: { author: { id: 'not a principal id', kind: 'human' } },
  unknown_stamp: { author: { id: 'p_unknown', kind: 'unknown', display: {}, provider: '' } },
  owner_local: { author: { id: 'p_owner_local', kind: 'human', display: {}, provider: 'local' } },
  owner_startup: { author: { id: 'p_owner_startup', kind: 'human', display: {}, provider: 'local' } },
  owner_tailnet: { author: { id: 'p_owner_tailnet', kind: 'human', display: {}, provider: 'tailscale' } },
  teammate_relay: { author: { id: 'p_teammate_relay', kind: 'human', display: {}, provider: 'kiro' } },
  teammate_relay_legacy: { author: { id: 'p_teammate_relay', kind: 'human', display: {} } },
  teammate_tailnet: { author: { id: 'p_teammate_tailnet', kind: 'human', display: {}, provider: 'tailscale' } },
  app: { author: { id: 'p_app', kind: 'system', display: {}, provider: 'system' } },
  other_app: { author: { id: 'p_other_app', kind: 'system', display: {}, provider: 'system' } },
  automation: { author: { id: 'p_cron', kind: 'system', display: {}, provider: 'system' } },
}

function viewer(name: string, preview: boolean): EditRuleViewer {
  const e = EDITORS[name]
  const mode = { viewer_ids: e.ids, edit_rule: { preview, is_owner: e.isOwner, relay_teammate: e.relayTeammate } } as unknown as CrewModeResponse
  const v = editRuleViewer(mode)
  if (!v) throw new Error('no viewer')
  return v
}

describe('edit rule (shared table with the gateway)', () => {
  it('has a row for every editor and author it names', () => {
    expect(cases.length).toBeGreaterThanOrEqual(30)
    for (const c of cases) {
      expect(EDITORS[c.editor], c.editor).toBeDefined()
      expect(AUTHORS[c.author], c.author).toBeDefined()
    }
  })
  for (const c of cases) {
    it(`${c.preview ? 'preview' : 'no preview'}: ${c.editor} on ${c.author}: ${c.why}`, () => {
      expect(mayEdit(viewer(c.editor, c.preview), AUTHORS[c.author])).toBe(c.expect)
    })
  }
  it('leaves every row editable until the mode route answers, or when an older gateway sends no edit_rule', () => {
    expect(mayEdit(null, AUTHORS.teammate_tailnet)).toBe(true)
    expect(editRuleViewer({ viewer_ids: ['p_x'] } as unknown as CrewModeResponse)).toBeNull()
  })
})
