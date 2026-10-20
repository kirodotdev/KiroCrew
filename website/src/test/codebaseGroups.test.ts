import { describe, it, expect } from 'vitest'
import { sessionCodebase, normalizeCodebasePath, codebaseLabel, groupByCodebase, isGatewayDefaultProject } from '../pages/chat-sidebar/codebaseGroups'
import type { ChatFolder } from '../types'
import type { Slot } from '../pages/chat-sidebar/types'

const folder = (id: string, over: Partial<ChatFolder> = {}): ChatFolder => ({ id, name: id, order: 0, ...over })
const slot = (key: string, over: Partial<Slot> = {}): Slot => ({ key, running: false, ...over })

describe('normalizeCodebasePath', () => {
  it('returns undefined for empty / whitespace / nullish', () => {
    expect(normalizeCodebasePath(undefined)).toBeUndefined()
    expect(normalizeCodebasePath(null)).toBeUndefined()
    expect(normalizeCodebasePath('')).toBeUndefined()
    expect(normalizeCodebasePath('   ')).toBeUndefined()
  })
  it('strips a single trailing separator so /repo and /repo/ match', () => {
    expect(normalizeCodebasePath('/repo/')).toBe('/repo')
    expect(normalizeCodebasePath('/repo')).toBe('/repo')
    expect(normalizeCodebasePath('C:\\proj\\')).toBe('C:\\proj')
  })
  it('does not fold case or merge distinct paths', () => {
    expect(normalizeCodebasePath('/Foo')).not.toBe(normalizeCodebasePath('/foo'))
  })
})

describe('codebaseLabel', () => {
  it('is the directory basename', () => {
    expect(codebaseLabel('/Users/me/p/repo')).toBe('repo')
    expect(codebaseLabel('C:\\work\\api')).toBe('api')
  })
  it('falls back to the path for a root with no basename', () => {
    expect(codebaseLabel('/')).toBe('/')
  })
})

describe('sessionCodebase', () => {
  const folders = [
    folder('root', { project_dir: '/p/root' }),
    folder('child', { parent_id: 'root' }),
    folder('own', { project_dir: '/p/own' }),
  ]
  it('prefers the slot own project over the folder', () => {
    expect(sessionCodebase({ project: '/p/explicit', folder_id: 'own' }, folders)).toBe('/p/explicit')
  })
  it('falls back to the folder project_dir (nearest ancestor)', () => {
    expect(sessionCodebase({ folder_id: 'child' }, folders)).toBe('/p/root')
    expect(sessionCodebase({ folder_id: 'own' }, folders)).toBe('/p/own')
  })
  it('is undefined with no project and no folder project_dir', () => {
    expect(sessionCodebase({ folder_id: undefined }, folders)).toBeUndefined()
    expect(sessionCodebase({ folder_id: 'missing' }, folders)).toBeUndefined()
  })
  it('normalizes the resolved path', () => {
    expect(sessionCodebase({ project: '/p/explicit/' }, folders)).toBe('/p/explicit')
  })
})

describe('gateway-assigned defaults are not a codebase', () => {
  const folders = [folder('repo', { project_dir: '/p/repo' })]

  it('treats the configured workspace default_project as no codebase', () => {
    // The gateway sets slot.project = dashboard.default_project on an unscoped
    // session; that is not a codebase the person chose.
    const ctx = { defaultProject: '/home/me/workspace', sessionFolderRoot: '' }
    expect(sessionCodebase({ project: '/home/me/workspace' }, [], ctx)).toBeUndefined()
    // A trailing slash on either side must not defeat the match.
    expect(sessionCodebase({ project: '/home/me/workspace/' }, [], ctx)).toBeUndefined()
    // ...and such a session falls back to its folder's project_dir if filed.
    expect(sessionCodebase({ project: '/home/me/workspace', folder_id: 'repo' }, folders, ctx)).toBe('/p/repo')
    // isGatewayDefaultProject agrees directly.
    expect(isGatewayDefaultProject('/home/me/workspace', ctx)).toBe(true)
    expect(isGatewayDefaultProject('/home/me/other', ctx)).toBe(false)
  })

  it('treats a path under session_folder_root as no codebase', () => {
    // With session_folder_root configured the gateway mints a per-session dir
    // under it; grouping on that would be one group per session.
    const ctx = { defaultProject: '', sessionFolderRoot: '/home/me/.kiro/sessions' }
    expect(sessionCodebase({ project: '/home/me/.kiro/sessions/2026-10-19-ab12' }, [], ctx)).toBeUndefined()
    // The root itself, and a trailing-slash root, also count.
    expect(sessionCodebase({ project: '/home/me/.kiro/sessions' }, [], ctx)).toBeUndefined()
    expect(isGatewayDefaultProject('/home/me/.kiro/sessions/2026-10-19-ab12', ctx)).toBe(true)
    // A sibling that merely shares a prefix string but is NOT under the root
    // (no separator boundary) is still a real codebase.
    expect(isGatewayDefaultProject('/home/me/.kiro/sessions-archive/x', ctx)).toBe(false)
    expect(sessionCodebase({ project: '/home/me/.kiro/sessions-archive/x' }, [], ctx)).toBe('/home/me/.kiro/sessions-archive/x')
    // A filed session under the root still falls back to its folder codebase.
    expect(sessionCodebase({ project: '/home/me/.kiro/sessions/2026-10-19-ab12', folder_id: 'repo' }, folders, ctx)).toBe('/p/repo')
  })

  it('uses the resolved default_project when the raw setting is unset', () => {
    // The reviewer's case: dashboard.default_project is UNSET, so the gateway
    // assigns slot.project = default_project_dir(workspace) (the realpath'd
    // workspace dir). The raw string is empty and cannot name it; the resolved
    // anchor exposed by the config GET must still match.
    const ctx = {
      defaultProject: '',
      sessionFolderRoot: '',
      defaultProjectResolved: '/real/workspace',
      sessionFolderRootResolved: '',
    }
    expect(isGatewayDefaultProject('/real/workspace', ctx)).toBe(true)
    expect(sessionCodebase({ project: '/real/workspace' }, [], ctx)).toBeUndefined()
    // A filed session there still falls back to its folder codebase.
    expect(sessionCodebase({ project: '/real/workspace', folder_id: 'repo' }, folders, ctx)).toBe('/p/repo')
    // An unrelated project is still a real codebase.
    expect(isGatewayDefaultProject('/real/other', ctx)).toBe(false)
  })

  it('prefers the resolved anchors over the raw strings (symlinked/~ setting)', () => {
    // The raw setting is a ~-form the browser cannot expand; the gateway stored
    // slot.project as the realpath. Comparing against the raw string alone would
    // miss it, so the resolved anchor is preferred.
    const ctx = {
      defaultProject: '~/workspace',
      sessionFolderRoot: '~/.kiro/sessions',
      defaultProjectResolved: '/home/me/workspace',
      sessionFolderRootResolved: '/home/me/.kiro/sessions',
    }
    expect(isGatewayDefaultProject('/home/me/workspace', ctx)).toBe(true)
    expect(isGatewayDefaultProject('/home/me/.kiro/sessions/2026-10-19-ab12', ctx)).toBe(true)
    // When the resolved anchor is present it REPLACES the raw string, so the
    // ~-form itself no longer matches — only the realpath'd slot.project does.
    expect(isGatewayDefaultProject('~/workspace', ctx)).toBe(false)
  })

  it('without a ctx, every explicit project is a codebase (back-compat)', () => {
    expect(sessionCodebase({ project: '/home/me/workspace' }, [])).toBe('/home/me/workspace')
    expect(isGatewayDefaultProject('/anything', undefined)).toBe(false)
  })

  it('an empty project is no codebase even with a ctx', () => {
    expect(isGatewayDefaultProject('', { defaultProject: '/x' })).toBe(true)
  })
})

describe('groupByCodebase', () => {
  const codebaseOf = (s: Slot): string | undefined =>
    ({ a: '/p/beta', b: '/p/alpha', c: '/p/beta' } as Record<string, string | undefined>)[s.key]

  it('buckets by codebase, orders groups by path, preserves input order in each', () => {
    const slots = [slot('a'), slot('b'), slot('c'), slot('d')]
    const groups = groupByCodebase(slots, codebaseOf, 'No codebase')
    expect(groups.map(g => g.path)).toEqual(['/p/alpha', '/p/beta', null])
    expect(groups.find(g => g.path === '/p/beta')!.slots.map(s => s.key)).toEqual(['a', 'c'])
    expect(groups.find(g => g.path === '/p/alpha')!.label).toBe('alpha')
  })

  it('puts the No codebase bucket last and uses the provided label', () => {
    const groups = groupByCodebase([slot('d'), slot('a')], codebaseOf, 'Nothing')
    expect(groups[groups.length - 1]).toMatchObject({ path: null, label: 'Nothing' })
    expect(groups[groups.length - 1].slots.map(s => s.key)).toEqual(['d'])
  })

  it('returns no No-codebase bucket when every session resolves', () => {
    const groups = groupByCodebase([slot('a'), slot('b')], codebaseOf, 'No codebase')
    expect(groups.some(g => g.path === null)).toBe(false)
  })
})
