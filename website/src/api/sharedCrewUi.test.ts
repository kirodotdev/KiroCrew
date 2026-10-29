import { describe, it, expect } from 'vitest'
import type { AcceptedWork, SharedDecision } from './sharedCrew'
import {
  isAcceptedWork,
  isUnknownPrincipal,
  limitScope,
  membershipPointer,
  mergeDecision,
  mergeWork,
  messageAuthor,
  bylineFor,
  bylineSelfId,
  setupPathStates,
  sharedAccessConfigured,
  ALL_UNAVAILABLE,
  principalInitials,
  principalName,
} from './sharedCrewUi'

const work = (over: Partial<AcceptedWork> = {}): AcceptedWork => ({
  work_id: 'w1', session_key: 's1', operation: 'chat_turn', state: 'queued', revision: 1,
  accepted_at: 100, expires_at: 200, deadline_at: null, reason: null, ...over,
})

const person = (name: string, login = '') => ({ id: 'p_' + 'a'.repeat(32), kind: 'human' as const, display: { name, login, captured_at: null } })

describe('mergeWork', () => {
  it('keeps one row per work id and never moves backwards', () => {
    let list = mergeWork([], work({ revision: 1 }))
    list = mergeWork(list, work({ revision: 3, state: 'running' }))
    // A replayed older frame after a reconnect changes nothing.
    list = mergeWork(list, work({ revision: 2, state: 'queued' }))
    list = mergeWork(list, work({ revision: 3, state: 'running' }))
    expect(list).toHaveLength(1)
    expect(list[0].state).toBe('running')
    expect(list[0].revision).toBe(3)
  })

  it('appends a different work id', () => {
    const list = mergeWork([work()], work({ work_id: 'w2' }))
    expect(list.map(w => w.work_id)).toEqual(['w1', 'w2'])
  })
})

describe('mergeDecision', () => {
  const d = (revision: number, state: SharedDecision['state']): SharedDecision => ({
    decision_id: 'd1', kind: 'approval', session_key: 's1', state, revision,
    initiator: person('Alice'), responder: state === 'resolved' ? person('Bob') : null,
    outcome: state === 'resolved' ? 'approved' : '', resolved_at: null,
  })
  it('lets a resolved decision replace pending, not the reverse', () => {
    let list = mergeDecision([], d(1, 'pending'))
    list = mergeDecision(list, d(2, 'resolved'))
    list = mergeDecision(list, d(1, 'pending'))
    expect(list).toHaveLength(1)
    expect(list[0].state).toBe('resolved')
  })
})

describe('principals', () => {
  it('reads Unknown for the unknown principal and missing data', () => {
    expect(isUnknownPrincipal(null)).toBe(true)
    expect(isUnknownPrincipal({ id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } })).toBe(true)
    expect(principalName({ id: 'p_unknown', kind: 'unknown', display: { name: 'Mallory', login: '', captured_at: null } })).toBe('')
  })

  it('falls back to the login and builds initials without fetching anything', () => {
    expect(principalName(person('', 'alice@example.test'))).toBe('alice@example.test')
    expect(principalInitials(person('Ada Lovelace'))).toBe('AL')
    expect(principalInitials(person('', 'bob@example.test'))).toBe('BE')
    expect(principalInitials(null)).toBe('?')
  })

  it('reads a server-stamped message author and ignores garbage', () => {
    expect(messageAuthor({ author: { id: 'p_x', kind: 'human', display: { name: 'Ann' } } })?.display.name).toBe('Ann')
    expect(messageAuthor({ author: 'Ann' })).toBeNull()
    expect(messageAuthor({ author: { kind: 'human' } })).toBeNull()
    expect(messageAuthor(undefined)).toBeNull()
    expect(messageAuthor({ author: { id: 'p_x', kind: 'admin', display: {} } })?.kind).toBe('unknown')
  })
})

describe('limits', () => {
  it('names whose limit holds a refusal', () => {
    expect(limitScope('principal_active_limit')).toBe('principal')
    expect(limitScope('crew_queue_full')).toBe('crew')
    expect(limitScope(null)).toBeNull()
  })

  it('prefers the capacity answer over the reason', () => {
    const cap = { limiting_scope: 'crew' } as Parameters<typeof limitScope>[1]
    expect(limitScope('principal_active_limit', cap)).toBe('crew')
  })

  it('recognises an accepted work body', () => {
    expect(isAcceptedWork(work())).toBe(true)
    expect(isAcceptedWork({ work_id: 'w' })).toBe(false)
    expect(isAcceptedWork(null)).toBe(false)
  })

  it('never turns a membership link into anything but a closed id', () => {
    expect(membershipPointer('tailscale_admin')).toBe('tailscale_admin')
    expect(membershipPointer('console')).toBe('console')
    expect(membershipPointer('')).toBeNull()
    expect(membershipPointer('https://evil.example/' as never)).toBeNull()
  })
})

describe('message bylines', () => {
  const person = (id: string, kind: 'human' | 'unknown' = 'human') => ({ id, kind, display: { name: id, login: '', captured_at: null } })
  const attribution = (id: string, kind: 'human' | 'unknown' = 'human') => ({ creator: person(id, kind), availability: 'recorded' as const, created_at: 1 })

  it('leaves only the viewer\'s own messages unlabelled', () => {
    const self = bylineSelfId(person('p_viewer'), attribution('p_creator'))
    expect(self).toBe('p_viewer')
    expect(bylineFor(person('p_viewer'), self)).toBeNull()
  })

  it('names the session creator to a viewer who did not create it', () => {
    const self = bylineSelfId(person('p_viewer'), attribution('p_creator'))
    expect(bylineFor(person('p_creator'), self)?.id).toBe('p_creator')
    expect(bylineFor(person('p_bob'), self)?.id).toBe('p_bob')
  })

  it('the creator reading their own session sees only the others named', () => {
    const self = bylineSelfId(person('p_creator'), attribution('p_creator'))
    expect(bylineFor(person('p_creator'), self)).toBeNull()
    expect(bylineFor(person('p_bob'), self)?.id).toBe('p_bob')
  })

  it('stands the creator in for the viewer until the viewer is known', () => {
    expect(bylineSelfId(undefined, attribution('p_creator'))).toBe('p_creator')
    expect(bylineSelfId(person('unknown', 'unknown'), attribution('p_creator'))).toBe('p_creator')
    expect(bylineFor(person('p_creator'), bylineSelfId(null, attribution('p_creator')))).toBeNull()
  })

  it('an Unknown author stays Unknown and is labelled', () => {
    expect(bylineFor(person('unknown', 'unknown'), 'p_viewer')?.kind).toBe('unknown')
  })

  it('labels nothing until the viewer or the creator is known', () => {
    expect(bylineSelfId(null, null)).toBe('')
    expect(bylineSelfId(null, attribution('unknown', 'unknown'))).toBe('')
    expect(bylineFor(person('p_bob'), '')).toBeNull()
    expect(bylineFor(null, 'p_viewer')).toBeNull()
  })
})

describe('shared-access setup paths', () => {
  it('an older gateway reads as no path configured', () => {
    expect(sharedAccessConfigured(ALL_UNAVAILABLE)).toBe(false)
    expect(setupPathStates(ALL_UNAVAILABLE).kiro_login.reason).toBe('not_in_this_build')
  })

  it('any available path counts as configured; degraded does not', () => {
    const paths = (tailscale: 'available' | 'degraded') => ({
      ...ALL_UNAVAILABLE,
      setup_paths: {
        tailscale: { state: tailscale, reason: '' as const },
        kiro_login: { state: 'unavailable' as const, reason: 'not_in_this_build' as const },
        systems_manager: { state: 'unavailable' as const, reason: 'not_in_this_build' as const },
      },
    })
    expect(sharedAccessConfigured(paths('available'))).toBe(true)
    expect(sharedAccessConfigured(paths('degraded'))).toBe(false)
  })
})
