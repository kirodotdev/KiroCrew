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
  continuesAuthorRun,
  setupPathStates,
  sharedAccessConfigured,
  ALL_UNAVAILABLE,
  principalInitials,
  principalName,
  heldLinks,
  waitingInvites,
  type PeopleRow,
} from './sharedCrewUi'
import type { TailscaleInvite } from './sharedCrewInvites'

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
    expect(principalInitials(person('', 'bob@example.test'))).toBe('BO')
    expect(principalInitials(null)).toBe('?')
  })

  it('builds initials from the part of an email before the @, or from a handle', () => {
    // A Tailscale login with no display name is shown as the email itself.
    expect(principalInitials(person('', 'priyashah@example.com'))).toBe('PR')
    expect(principalInitials(person('jordan.lee@example.com'))).toBe('JL')
    expect(principalInitials(person('', 'sam_okafor@example.com'))).toBe('SO')
    // A bare handle, with and without a separator.
    expect(principalInitials(person('priyashah'))).toBe('PR')
    expect(principalInitials(person('samokafor'))).toBe('SA')
    expect(principalInitials(person('jordan-lee'))).toBe('JL')
    // A relay label: a display name, an email, or the id fallback with spaces.
    expect(principalInitials(person('Ada Lovelace'))).toBe('AL')
    expect(principalInitials(person('ada.lovelace@example.com'))).toBe('AL')
    expect(principalInitials(person('Kiro user ...a1b2c3'))).toBe('KU')
  })

  it('never crashes on a one-letter, separator-only or empty label', () => {
    expect(principalInitials(person('x'))).toBe('X')
    expect(principalInitials(person('', 'x@example.com'))).toBe('X')
    expect(principalInitials(person('@'))).toBe('?')
    expect(principalInitials(person('._-'))).toBe('?')
    expect(principalInitials(person('', ''))).toBe('?')
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

  describe('continuesAuthorRun', () => {
    const user = (id: string | null, kind: 'human' | 'unknown' = 'human') => ({ role: 'user', meta: id ? { author: person(id, kind) } : {} })
    const row = (role: string, extra: Record<string, unknown> = {}) => ({ role, ...extra })
    const at = (rows: Array<{ role: string; meta?: Record<string, unknown> }>) => rows.map((_, i) => continuesAuthorRun(rows, i))

    it('continues on the same author and breaks on a new one', () => {
      expect(at([user('p_a'), user('p_a'), user('p_b'), user('p_a'), user('p_a')])).toEqual([false, true, false, false, true])
    })

    it('breaks on any drawn row between: an agent reply, a tool line, a notice, a system card', () => {
      for (const between of [row('assistant'), row('tool'), row('notice'), row('system', { kind: 'stop_event' }), row('system', { meta: { kind: 'skill_load' } })]) {
        expect(at([user('p_a'), between, user('p_a')])[2]).toBe(false)
      }
    })

    it('reads past state rows no surface draws', () => {
      expect(at([user('p_a'), row('done'), row('queued'), user('p_a')])[3]).toBe(true)
    })

    it('never continues an unstamped or an Unknown author', () => {
      expect(at([user(null), user(null)])).toEqual([false, false])
      expect(at([user('unknown', 'unknown'), user('unknown', 'unknown')])).toEqual([false, false])
      expect(at([user(null), user('p_a')])).toEqual([false, false])
    })

    it('a steer above, retracted or confirmed, draws no byline, so the next message names its sender', () => {
      const steer = (steerState: string) => ({ role: 'user', meta: { author: person('p_a'), steer: true, steerState } })
      for (const state of ['retracted', 'settled', 'written']) {
        expect(at([steer(state), user('p_a')])[1]).toBe(false)
      }
    })

    it('an app\'s send above is labelled with the app, so the next message names its sender', () => {
      const appSend = { role: 'user', meta: { author: person('p_a'), turnActor: 'app', appLabel: 'notes/1' } }
      expect(at([appSend, user('p_a')])[1]).toBe(false)
    })
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
      },
    })
    expect(sharedAccessConfigured(paths('available'))).toBe(true)
    expect(sharedAccessConfigured(paths('degraded'))).toBe(false)
  })
})

describe('waitingInvites: each person is one row', () => {
  const seen = (login: string): PeopleRow => ({ principal: person('Kyle', login), sources: ['observed'] })
  const member = (login: string): PeopleRow => ({
    ...seen(login),
    member: { alias: '', avatar_id: '', profile_pic_url: '', invite_id: 'i0' },
  })
  const invite = (over: Partial<TailscaleInvite> = {}): TailscaleInvite => ({
    id: 'i1', transport: 'tailscale', kind: 'email', email: 'kyle@example.test', multi_use: false, alias: '', avatar_id: '',
    created_at: 1, status: 'pending', accepted_login: '', ...over,
  })

  it('keeps a pending invite for someone who is not in the list', () => {
    expect(waitingInvites([member('other@example.test')], [invite()]).map(i => i.id)).toEqual(['i1'])
  })

  it('lists every pending email invite the server returns, even for a current member', () => {
    expect(waitingInvites([member('Kyle@Example.test')], [invite({ email: ' kyle@example.test ' })]).map(i => i.id)).toEqual(['i1'])
  })

  it('keeps the invite of a row that is only seen or named, which is not a member', () => {
    expect(waitingInvites([seen('kyle@example.test')], [invite()]).map(i => i.id)).toEqual(['i1'])
    expect(waitingInvites([{ ...seen('kyle@example.test'), sources: ['tailscale_share'] }], [invite()]).map(i => i.id)).toEqual(['i1'])
  })

  it('lists an accepted invite until its accepting login is a current member, so a share outliving a removal stays removable', () => {
    const accepted = invite({ status: 'accepted', accepted_login: 'Kyle@Example.test' })
    expect(waitingInvites([], [accepted]).map(i => i.id)).toEqual(['i1'])
    expect(waitingInvites([seen('other@example.test')], [accepted]).map(i => i.id)).toEqual(['i1'])
    // A person removed through another invite is seen but no member: this share is still live.
    expect(waitingInvites([seen('kyle@example.test')], [accepted]).map(i => i.id)).toEqual(['i1'])
    expect(waitingInvites([member('kyle@example.test')], [accepted])).toEqual([])
  })

  it('leaves a link invite to the invite dialog, since it names nobody', () => {
    expect(waitingInvites([member('kyle@example.test')], [invite({ id: 'l1', kind: 'link', email: '' })])).toEqual([])
  })

  it('takes invites from any sharing path', () => {
    const other = { ...invite({ id: 's1' }), transport: 'ssm' } as unknown as TailscaleInvite
    expect(waitingInvites([], [invite(), other]).map(i => i.id)).toEqual(['i1', 's1'])
  })

  it('a member row with no login matches no invite', () => {
    expect(waitingInvites([member('')], [invite()]).map(i => i.id)).toEqual(['i1'])
  })
})

describe('heldLinks: a link stays removable until its person is a member', () => {
  const member = (login: string): PeopleRow => ({
    principal: person('Kyle', login), sources: ['observed'], member: { alias: '', avatar_id: '', profile_pic_url: '', invite_id: 'l1' },
  })
  const link = (over: Partial<TailscaleInvite> = {}): TailscaleInvite => ({
    id: 'l1', transport: 'tailscale', kind: 'link', email: '', multi_use: false, alias: '', avatar_id: '', created_at: 1,
    status: 'pending', accepted_login: '', ...over,
  })

  it('keeps a reusable link and a single-use link nobody used', () => {
    expect(heldLinks([], [link({ multi_use: true, status: 'accepted' }), link({ id: 'l2' })]).map(i => i.id)).toEqual(['l1', 'l2'])
  })

  it('keeps an accepted single-use link until its accepting login is a member', () => {
    const accepted = link({ status: 'accepted', accepted_login: 'Kyle@Example.test' })
    expect(heldLinks([], [accepted]).map(i => i.id)).toEqual(['l1'])
    expect(heldLinks([member('kyle@example.test')], [accepted])).toEqual([])
  })

  it('never lists an email invite', () => {
    expect(heldLinks([], [link({ kind: 'email', email: 'a@example.test' })])).toEqual([])
  })
})
