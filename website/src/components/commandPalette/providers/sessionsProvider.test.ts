import { describe, it, expect, vi } from 'vitest'
import {
  createSessionsProvider,
  SESSIONS_MIN_QUERY_CHARS,
  type SessionsProviderDeps,
  type SessionSearchResponse,
  type SessionRef,
} from './sessionsProvider'

/**
 * Unit tests for the pure {@link createSessionsProvider} factory
 * (Search Everywhere). Exercises the provider's public contract with a
 * mock `fetchSessions` + open spies — no React hooks, React-Query, or Redux.
 *
 * The provider keeps backend hits even when the client-side fuzzy pass does not
 * match (score 0); ties in score fall back to the BACKEND's returned order
 * (issue #4568), so test data either crafts distinct scores or asserts the
 * response order directly.
 */

function deps(over: Partial<SessionsProviderDeps> = {}): {
  d: SessionsProviderDeps
  fetchSessions: ReturnType<typeof vi.fn>
  openSession: ReturnType<typeof vi.fn>
  openInSplit: ReturnType<typeof vi.fn>
} {
  const fetchSessions = vi.fn(
    async (): Promise<SessionSearchResponse> => ({ sessions: [] }),
  )
  const openSession = vi.fn()
  const openInSplit = vi.fn()
  const d: SessionsProviderDeps = {
    fetchSessions: over.fetchSessions ?? fetchSessions,
    openSession: over.openSession ?? openSession,
    openInSplit: 'openInSplit' in over ? over.openInSplit : openInSplit,
  }
  return { d, fetchSessions, openSession, openInSplit }
}

describe('createSessionsProvider — identity & metadata', () => {
  it('exposes the sessions provider id, label, and an icon node', () => {
    const { d } = deps()
    const p = createSessionsProvider(d)
    expect(p.id).toBe('sessions')
    expect(p.label).toBe('Sessions')
    expect(p.icon).toBeTruthy()
  })
})

describe('createSessionsProvider — sub-threshold queries cost no round trip', () => {
  // `/api/sessions/search` returns an empty list below SEARCH_MIN_CHARS (2), and
  // serving that empty list still made the backend walk the corpus. Skipping the
  // fetch is behavior-preserving because the answer was already always empty.
  it('does not fetch for a one-character query', async () => {
    const { d, fetchSessions } = deps()
    const results = await createSessionsProvider(d).search('k')
    expect(results).toEqual([])
    expect(fetchSessions).not.toHaveBeenCalled()
  })

  it('does not fetch for a query that is only whitespace-padded to length', async () => {
    const { d, fetchSessions } = deps()
    expect(await createSessionsProvider(d).search('  k  ')).toEqual([])
    expect(fetchSessions).not.toHaveBeenCalled()
  })

  it('still fetches the recents listing for an empty query', async () => {
    const { d, fetchSessions } = deps()
    await createSessionsProvider(d).search('')
    expect(fetchSessions).toHaveBeenCalledWith('')
  })

  it('fetches once the query reaches the threshold', async () => {
    const { d, fetchSessions } = deps()
    await createSessionsProvider(d).search('ki')
    expect(fetchSessions).toHaveBeenCalledWith('ki')
  })

  it('declares minQueryChars equal to the constant the short-circuit enforces (issue #1830)', async () => {
    // The palette reads `minQueryChars` to render the "keep typing" empty
    // state. It must be the SAME value the search short-circuit uses, or the
    // copy and the behavior drift apart. Pin both the declaration and the
    // boundary behavior to the exported constant.
    const { d, fetchSessions } = deps()
    const p = createSessionsProvider(d)
    expect(p.minQueryChars).toBe(SESSIONS_MIN_QUERY_CHARS)
    // Boundary: one char below the declared minimum never fetches...
    await p.search('x'.repeat(SESSIONS_MIN_QUERY_CHARS - 1))
    expect(fetchSessions).not.toHaveBeenCalled()
    // ...and exactly the declared minimum does.
    await p.search('x'.repeat(SESSIONS_MIN_QUERY_CHARS))
    expect(fetchSessions).toHaveBeenCalledTimes(1)
  })
})

describe('createSessionsProvider — result mapping', () => {
  it('maps a backend session to a Result with id, title, subtitle, and highlight indices', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [{ key: 's-1', title: 'Session Grid', agent: 'kirocrew' }],
      }),
    )
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider(d)

    const results = await p.search('grid')
    expect(results).toHaveLength(1)
    const r = results[0]
    expect(r.id).toBe('sessions:s-1')
    expect(r.providerId).toBe('sessions')
    expect(r.title).toBe('Session Grid')
    expect(r.subtitle).toBe('kirocrew')
    expect(r.score).toBeGreaterThan(0)
    // 'grid' lands on the second word — indices into the title, ascending.
    expect(r.indices.length).toBe(4)
    for (let i = 1; i < r.indices.length; i++) {
      expect(r.indices[i]).toBeGreaterThan(r.indices[i - 1])
    }
  })

  it('falls back to the session key when the title is missing', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({ sessions: [{ key: 'abc123' }] }),
    )
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider(d)

    const [r] = await p.search('abc')
    expect(r.title).toBe('abc123')
    expect(r.subtitle).toBeUndefined()
  })

  it('keeps non-matching backend hits (neutral score) rather than dropping them', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [
          { key: 'a', title: 'Session Grid' },
          { key: 'b', title: 'Cron Jobs' },
        ],
      }),
    )
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider(d)

    // 'grid' matches 'Session Grid' (>0) but not 'Cron Jobs' (0) — distinct
    // scores, so the title-match sort puts the match first and keeps both.
    const results = await p.search('grid')
    expect(results).toHaveLength(2)
    expect(results[0].title).toBe('Session Grid')
    expect(results[0].score).toBeGreaterThan(results[1].score)
  })
})

describe('createSessionsProvider — activation', () => {
  it('Enter (onActivate) opens the session with its {key,title} ref', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [{ key: 's-1', title: 'My Session' }],
      }),
    )
    const openSession = vi.fn()
    const { d } = deps({ fetchSessions, openSession })
    const p = createSessionsProvider(d)

    const [r] = await p.search('my')
    r.onActivate()
    const ref: SessionRef = { key: 's-1', title: 'My Session' }
    expect(openSession).toHaveBeenCalledTimes(1)
    expect(openSession).toHaveBeenCalledWith(ref)
  })

  it('binds ⌘Enter (onCmdActivate) to openInSplit when supplied', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [{ key: 's-1', title: 'Grid Me' }],
      }),
    )
    const openInSplit = vi.fn()
    const { d } = deps({ fetchSessions, openInSplit })
    const p = createSessionsProvider(d)

    const [r] = await p.search('grid')
    expect(r.onCmdActivate).toBeTypeOf('function')
    r.onCmdActivate?.()
    expect(openInSplit).toHaveBeenCalledWith({ key: 's-1', title: 'Grid Me' })
  })

  it('leaves ⌘Enter unbound when no openInSplit is supplied', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [{ key: 's-1', title: 'Grid Me' }],
      }),
    )
    const { d } = deps({ fetchSessions, openInSplit: undefined })
    const p = createSessionsProvider(d)

    const [r] = await p.search('grid')
    expect(r.onCmdActivate).toBeUndefined()
  })
})

describe('createSessionsProvider — fetch wiring', () => {
  it('passes the trimmed query through to fetchSessions', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({ sessions: [] }),
    )
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider(d)

    await p.search('  hello  ')
    expect(fetchSessions).toHaveBeenCalledWith('hello')
  })

  it('tolerates a malformed response envelope (no sessions array)', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({}) as SessionSearchResponse,
    )
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider(d)

    const results = await p.search('x')
    expect(results).toEqual([])
  })
})

describe('createSessionsProvider — folder-fetch failure is distinct from search failure', () => {
  it('still returns session results when the folder fetch rejects (chip omitted, failure logged, not conflated with "no folders")', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [{ key: 's-1', title: 'My Session', folder_id: 'f-1' }],
      }),
    )
    const fetchFolders = vi.fn(async (): Promise<{ id: string; name: string }[]> => {
      throw new Error('folders down')
    })
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider({ ...d, fetchFolders })

    const results = await p.search('my')
    // A folders failure must NEVER blank the search results…
    expect(results).toHaveLength(1)
    expect(results[0].title).toBe('My Session')
    // …the folder chip is simply omitted (folder unresolved)…
    expect(results[0].folder).toBeUndefined()
    // …and the failure is surfaced (logged), not silently swallowed as "no folders".
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it('propagates a session-search rejection — distinct from the swallowed folder failure — so the palette can render its error state', async () => {
    const fetchSessions = vi.fn(async (): Promise<SessionSearchResponse> => {
      throw new Error('search down')
    })
    const { d } = deps({ fetchSessions })
    const p = createSessionsProvider(d)

    await expect(p.search('my')).rejects.toThrow('search down')
  })
})

describe('createSessionsProvider — backend relevance order is the score tiebreak (issue #4568)', () => {
  it('preserves backend order for body-only hits (all scores 0) instead of alphabetizing', async () => {
    // Titles are in REVERSE-alphabetical order and share no characters with the
    // query, so every row is a body hit with score 0. The backend ranked these
    // by relevance (search_sessions weighting); the old name tiebreak returned
    // them alphabetized ('Alpha…' first). The response order must come back
    // untouched — the clearest user-visible case is searching a PR/issue
    // number, which lives in transcripts but almost never in a title.
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [
          { key: 'z', title: 'Zebra rollout', snippet: 'discussed 4568 here' },
          { key: 'm', title: 'Muffin sync', snippet: 'follow-up on 4568' },
          { key: 'a', title: 'Alpha rollout', snippet: '4568 mentioned once' },
        ],
      }),
    )
    const { d } = deps({ fetchSessions })
    const results = await createSessionsProvider(d).search('4568')
    expect(results).toHaveLength(3)
    expect(results.every(r => r.score === 0)).toBe(true)
    // Backend order, NOT ['Alpha rollout', 'Muffin sync', 'Zebra rollout'].
    expect(results.map(r => r.title)).toEqual(['Zebra rollout', 'Muffin sync', 'Alpha rollout'])
  })

  it('still ranks a title match first even when the backend returned it last (bias preserved)', async () => {
    // The title-match-first bias is deliberate and must survive the tiebreak
    // change: a title hit outranks body-only hits regardless of backend index.
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [
          { key: 'b1', title: 'Unrelated alpha', snippet: 'body mentions grid' },
          { key: 'b2', title: 'Unrelated beta', snippet: 'grid again' },
          { key: 't', title: 'grid work' },
        ],
      }),
    )
    const { d } = deps({ fetchSessions })
    const results = await createSessionsProvider(d).search('grid')
    expect(results).toHaveLength(3)
    expect(results[0].title).toBe('grid work')
    expect(results[0].score).toBeGreaterThan(0)
    // The remaining body-only rows keep their backend order between themselves.
    expect(results.slice(1).map(r => r.title)).toEqual(['Unrelated alpha', 'Unrelated beta'])
  })
})

describe('createSessionsProvider — federated (remote-instance) rows', () => {
  // Remote rows come back from /api/instances/search-sessions tagged with
  // instance_id/_name. Their contract differs from local rows in four ways,
  // each guarding a real failure mode:
  //  - id is namespaced by instance so two gateways' same-keyed sessions
  //    cannot collide in the palette's keyed list;
  //  - Enter routes through the instance switcher (ref.instanceId), never
  //    resumeFromHistory, which would open a same-keyed UNRELATED local chat;
  //  - ⌘Enter (local split grid) is unbound — the transcript lives elsewhere;
  //  - snippet highlight offsets are omitted because the instance-name prefix
  //    shifts them (wrong characters would be marked).
  const remoteFixture = async (): Promise<SessionSearchResponse> => ({
    sessions: [
      { key: 'chat-1', title: 'deploy notes', agent: 'kirocrew' },
      {
        key: 'chat-1', // deliberately same key as the local row
        title: 'deploy notes',
        snippet: 'we planned the deploy here',
        instance_id: 'inst-a',
        instance_name: 'clouddeskARM',
      },
    ],
  })

  it('namespaces the remote row id by instance so same-keyed rows cannot collide', async () => {
    const { d } = deps({ fetchSessions: vi.fn(remoteFixture) })
    const results = await createSessionsProvider(d).search('deploy')
    const ids = results.map(r => r.id)
    expect(ids).toContain('sessions:chat-1')
    expect(ids).toContain('sessions:inst-a:chat-1')
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('prefixes the remote subtitle with the instance name and SHIFTS snippet highlights by the prefix', async () => {
    const { d } = deps({ fetchSessions: vi.fn(remoteFixture) })
    const results = await createSessionsProvider(d).search('deploy')
    const remote = results.find(r => r.id === 'sessions:inst-a:chat-1')!
    expect(remote.subtitle).toBe('clouddeskARM · we planned the deploy here')
    // Offsets were computed against the bare snippet; the prefix shifts the
    // snippet within the subtitle, so the highlight indices shift with it and
    // must land exactly on 'deploy' in the RENDERED string.
    const idx = remote.subtitleIndices!
    expect(idx.length).toBe('deploy'.length)
    const highlighted = idx.map(i => remote.subtitle![i]).join('')
    expect(highlighted).toBe('deploy')
    expect(remote.subtitle!.slice(idx[0], idx[idx.length - 1] + 1)).toBe('deploy')
    // The local sibling has no snippet — subtitle falls back to the agent name.
    const local = results.find(r => r.id === 'sessions:chat-1')!
    expect(local.subtitle).toBe('kirocrew')
  })

  it('falls back to instance_id in the subtitle when the instance name is absent', async () => {
    const fetchSessions = vi.fn(
      async (): Promise<SessionSearchResponse> => ({
        sessions: [{ key: 'k', title: 'deploy', instance_id: 'inst-b' }],
      }),
    )
    const { d } = deps({ fetchSessions })
    const results = await createSessionsProvider(d).search('deploy')
    expect(results[0].subtitle).toBe('inst-b')
  })

  it('Enter on a remote row passes instanceId to openSession; a local row passes none', async () => {
    const { d, openSession } = deps({ fetchSessions: vi.fn(remoteFixture) })
    const results = await createSessionsProvider(d).search('deploy')

    results.find(r => r.id === 'sessions:inst-a:chat-1')!.onActivate?.()
    expect(openSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ key: 'chat-1', instanceId: 'inst-a' }),
    )

    results.find(r => r.id === 'sessions:chat-1')!.onActivate?.()
    expect(openSession).toHaveBeenLastCalledWith(
      expect.objectContaining({ key: 'chat-1', instanceId: undefined }),
    )
  })

  it('makes ⌘Enter inert on remote rows while keeping the split opener on local rows', async () => {
    const { d, openSession, openInSplit } = deps({ fetchSessions: vi.fn(remoteFixture) })
    const results = await createSessionsProvider(d).search('deploy')
    // A bare `undefined` would make the palette's dispatchEnter fall back to
    // onActivate (switching panes on ⌘Enter); the contract is an explicit
    // no-op that triggers neither open path.
    const remote = results.find(r => r.id === 'sessions:inst-a:chat-1')!
    expect(remote.onCmdActivate).toBeDefined()
    remote.onCmdActivate?.()
    expect(openSession).not.toHaveBeenCalled()
    expect(openInSplit).not.toHaveBeenCalled()
    // Local rows keep the real split opener.
    results.find(r => r.id === 'sessions:chat-1')!.onCmdActivate?.()
    expect(openInSplit).toHaveBeenCalledWith(expect.objectContaining({ key: 'chat-1' }))
  })
})
