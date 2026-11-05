import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  resolveSessionKinds,
  sessionKindDescriptor,
  MAX_SESSION_KINDS_PER_APP,
  type SessionKindAppRecord,
} from '../hooks/sessionKindRegistry'

const kind = (over: Record<string, unknown> = {}) => ({
  id: 'project', title: 'Project app', menuLabel: 'New project app',
  menuDescription: 'A React app built for this piece of work',
  icon: 'AppWindow', entry: 'dist/pane.mjs', defaultAgent: 'kirocrew-conductor', ...over,
})
const app = (name: string, kinds: unknown, over: Partial<SessionKindAppRecord> = {}): SessionKindAppRecord => ({
  name, enabled: true, manifest: { contributes: { sessionKinds: kinds } }, ...over,
})

let warn: ReturnType<typeof vi.spyOn>
beforeEach(() => { warn = vi.spyOn(console, 'warn').mockImplementation(() => {}) })
afterEach(() => warn.mockRestore())

describe('resolveSessionKinds', () => {
  it('resolves an enabled app kind with every manifest field', () => {
    expect(resolveSessionKinds([app('project-apps', [kind()])])).toEqual([{
      appName: 'project-apps', kindId: 'project', title: 'Project app', menuLabel: 'New project app',
      menuDescription: 'A React app built for this piece of work', icon: 'AppWindow',
      entry: 'dist/pane.mjs', defaultAgent: 'kirocrew-conductor',
    }])
    expect(warn).not.toHaveBeenCalled()
  })

  it('leaves the optional fields out when the manifest omits them', () => {
    const [d] = resolveSessionKinds([app('p', [kind({ menuDescription: undefined, icon: undefined, defaultAgent: '' })])])
    expect(d).not.toHaveProperty('menuDescription')
    expect(d).not.toHaveProperty('defaultAgent')
    expect(d.icon).toBe('')
  })

  it('is empty with no apps, or none contributing', () => {
    expect(resolveSessionKinds([])).toEqual([])
    expect(resolveSessionKinds([app('x', [])])).toEqual([])
    expect(resolveSessionKinds([{ name: 'p', enabled: true }])).toEqual([])
    expect(resolveSessionKinds([{ name: 'p', enabled: true, manifest: {} }])).toEqual([])
  })

  it('skips a disabled or unavailable app (enable is the user opt-in)', () => {
    expect(resolveSessionKinds([app('p', [kind()], { enabled: false })])).toEqual([])
    expect(resolveSessionKinds([app('p', [kind()], { enabled: undefined })])).toEqual([])
    expect(resolveSessionKinds([app('p', [kind()], { unavailable: { reason: 'PermissionError: denied' } })])).toEqual([])
  })

  it('orders kinds by app name in byte order, keeping each app’s declared order', () => {
    const out = resolveSessionKinds([
      app('zeta', [kind({ id: 'b' }), kind({ id: 'a' })]),
      app('alpha', [kind({ id: 'z' })]),
    ]).map(d => `${d.appName}:${d.kindId}`)
    expect(out).toEqual(['alpha:z', 'zeta:b', 'zeta:a'])
  })

  it('warns and skips an id outside the manifest grammar', () => {
    for (const id of ['', 'Project', '1project', 'pro ject', 'p'.repeat(65), 7, null]) {
      expect(resolveSessionKinds([app('p', [kind({ id })])])).toEqual([])
    }
    expect(resolveSessionKinds([app('p', [kind({ id: 'p'.repeat(64) })])])).toHaveLength(1)
    expect(warn).toHaveBeenCalled()
  })

  it('warns and skips a declaration missing title, menuLabel or entry', () => {
    for (const field of ['title', 'menuLabel', 'entry']) {
      expect(resolveSessionKinds([app('p', [kind({ [field]: '' })])])).toEqual([])
      expect(resolveSessionKinds([app('p', [kind({ [field]: 3 })])])).toEqual([])
    }
    expect(warn).toHaveBeenCalled()
  })

  it('warns and skips a duplicate id within one app, but not across apps', () => {
    const out = resolveSessionKinds([app('a', [kind(), kind({ title: 'Second' })]), app('b', [kind()])])
    expect(out.map(d => `${d.appName}:${d.kindId}:${d.title}`)).toEqual(['a:project:Project app', 'b:project:Project app'])
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('caps one app at MAX_SESSION_KINDS_PER_APP, warns, and still lets the next app contribute', () => {
    expect(MAX_SESSION_KINDS_PER_APP).toBe(4)
    const many = Array.from({ length: MAX_SESSION_KINDS_PER_APP + 2 }, (_, i) => kind({ id: `k${i}` }))
    const out = resolveSessionKinds([app('alpha', many), app('beta', [kind({ id: 'only' })])])
    expect(out.filter(d => d.appName === 'alpha')).toHaveLength(MAX_SESSION_KINDS_PER_APP)
    expect(out.filter(d => d.appName === 'beta').map(d => d.kindId)).toEqual(['only'])
    expect(warn).toHaveBeenCalled()
  })

  it('never throws on hand-edited shapes', () => {
    expect(resolveSessionKinds([app('p', {})])).toEqual([])
    expect(resolveSessionKinds([app('p', 'project')])).toEqual([])
    expect(resolveSessionKinds([app('p', [null, 'x', 4, kind()])]).map(d => d.kindId)).toEqual(['project'])
    expect(resolveSessionKinds(null as never)).toEqual([])
    expect(resolveSessionKinds({} as never)).toEqual([])
  })
})

describe('resolveSessionKinds: who owns the default agent', () => {
  const withAgents = (agents: unknown, defaultAgent = 'project-conductor') =>
    resolveSessionKinds([{ ...app('project-apps', [kind({ defaultAgent })]), manifest: { agents, contributes: { sessionKinds: [kind({ defaultAgent })] } } }])[0]

  it('marks an agent the app ships, by its agent file’s stem', () => {
    expect(withAgents(['agents/project-conductor.json']).appOwnsDefaultAgent).toBe(true)
    expect(withAgents(['agents\\project-conductor.json']).appOwnsDefaultAgent).toBe(true)
    expect(withAgents(['project-conductor.json', 'agents/other.json']).appOwnsDefaultAgent).toBe(true)
  })

  it('leaves a core agent, or a near-miss name, unowned', () => {
    expect(withAgents(['agents/project-conductor.json'], 'kirocrew-conductor')).not.toHaveProperty('appOwnsDefaultAgent')
    expect(withAgents(['agents/project-conductor.yaml'])).not.toHaveProperty('appOwnsDefaultAgent')
    expect(withAgents(['agents/project-conductor-v2.json'])).not.toHaveProperty('appOwnsDefaultAgent')
    expect(withAgents(undefined)).not.toHaveProperty('appOwnsDefaultAgent')
    expect(withAgents('agents/project-conductor.json')).not.toHaveProperty('appOwnsDefaultAgent')
    expect(withAgents([7, null])).not.toHaveProperty('appOwnsDefaultAgent')
  })

  it('never marks a kind with no default agent', () => {
    expect(withAgents(['agents/project-conductor.json'], '')).not.toHaveProperty('appOwnsDefaultAgent')
  })
})

describe('sessionKindDescriptor', () => {
  const apps = [app('project-apps', [kind(), kind({ id: 'other', entry: 'other.mjs' })]), app('off', [kind()], { enabled: false })]

  it('finds the live descriptor for a binding', () => {
    expect(sessionKindDescriptor(apps, 'project-apps', 'other')?.entry).toBe('other.mjs')
  })

  it('answers undefined for a missing app, a disabled app, or a kind no longer declared', () => {
    expect(sessionKindDescriptor(apps, 'gone', 'project')).toBeUndefined()
    expect(sessionKindDescriptor(apps, 'off', 'project')).toBeUndefined()
    expect(sessionKindDescriptor(apps, 'project-apps', 'retired')).toBeUndefined()
  })
})
