import { describe, it, expect } from 'vitest'
import { showsTeamAttribution } from './sharedCrewMode'
import { someoneJoined, type PeopleRow } from './sharedCrewUi'

const row = (over: Partial<PeopleRow> & { id?: string }): PeopleRow => ({
  principal: { id: over.id ?? 'p_tm', kind: 'human', display: { name: 'Tam', login: '', captured_at: null } },
  sources: ['observed'],
  is_viewer: false,
  ...over,
})

describe('someoneJoined', () => {
  it('counts a non-owner this Crew saw on a request, the viewing teammate included', () => {
    expect(someoneJoined([row({})])).toBe(true)
    expect(someoneJoined([row({ is_owner: true, id: 'p_me' }), row({ is_viewer: true })])).toBe(true)
  })

  it.each([
    ['nothing', undefined],
    ['no rows', []],
    ['only the owner', [row({ is_owner: true })]],
    ['a session creator who never connected (a Slack sender)', [row({ sources: ['session_creator'] })]],
    ['an invite not used yet', [row({ sources: ['tailscale_share'] })]],
    ['a directory entry', [row({ sources: ['directory'] })]],
    ['the Unknown principal', [row({ id: 'p_unknown', principal: { id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } } })]],
  ] as const)('does not count %s', (_what, rows) => {
    expect(someoneJoined(rows as readonly PeopleRow[] | undefined)).toBe(false)
  })
})

describe('showsTeamAttribution', () => {
  const on = { previewOn: true, attributes: true, mode: 'network' as const, joined: true }

  it('shows with the preview on, attribution, a team mode and someone joined', () => {
    expect(showsTeamAttribution(on)).toBe(true)
    expect(showsTeamAttribution({ ...on, mode: 'console' })).toBe(true)
  })

  it.each([
    ['the preview is off', { previewOn: false }],
    ['the Crew cannot attribute', { attributes: false }],
    ['the mode is not read yet', { mode: null }],
    ['the Crew is personal', { mode: 'personal' as const }],
    ['nobody besides the owner joined', { joined: false }],
  ])('hides when %s', (_why, over) => {
    expect(showsTeamAttribution({ ...on, ...over })).toBe(false)
  })
})
