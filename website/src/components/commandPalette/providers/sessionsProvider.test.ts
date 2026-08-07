import { describe, it, expect, vi } from 'vitest'
import {
  createSessionsProvider,
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
 * match (score 0), so test data is crafted so each result set has distinct
 * scores (the `compareByScoreThenName` sort only inspects names on a tie).
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
