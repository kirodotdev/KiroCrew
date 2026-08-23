/**
 * Tests for the Auto Triage Pipeline API client (`./api`).
 *
 * The module reads THROUGH Issue Radar's crew-fabric seam and PROMISES to be
 * forward-tolerant: `crewFabric` and `listConnectedRepos` never throw on "no
 * data yet" — a transport failure, any non-2xx, a non-JSON body, or a payload
 * from a newer/wrong schema all collapse to the same normalized empty result.
 * These tests assert that contract (the request built, the request query, and
 * the guaranteed shape on every degraded path), not merely line execution.
 *
 * Fetch-mocking idiom copied from `src/test/apiRewind.test.ts` /
 * `src/test/devFleetApi.test.ts`: spy on `globalThis.fetch`, restore after each.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  autoTriagePipelineApi,
  loadStoredPreference,
  saveRepoPreference,
  CREW_PHASES,
  CREW_FABRIC_SCHEMA,
  REPO_PREFERENCE_KEY,
  ISSUE_RADAR_ACTIVE_REPO_KEY,
  type CrewFabricResponse,
  type RepoRef,
} from './api'

const ISSUE_RADAR_API = '/api/apps/issue-radar'

/** Resolve a fetch mock to a JSON body at the given status. */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Resolve a fetch mock to a raw (possibly non-JSON) body at the given status. */
function rawResponse(body: string, status = 200): Response {
  return new Response(body, { status })
}

/** The URL fetch was called with on its first (or only) invocation. */
function calledUrl(spy: ReturnType<typeof vi.spyOn>): string {
  const [url] = spy.mock.calls[0] as [string, RequestInit?]
  return url
}

describe('autoTriagePipelineApi.crewFabric', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('GETs the crew/fabric endpoint with owner/repo query and same-origin credentials', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ schema: 1, owner: 'acme', repo: 'demo-repo', phases: [], items: [] }),
    )

    await autoTriagePipelineApi.crewFabric({ owner: 'acme', repo: 'demo-repo' })

    expect(fetchSpy).toHaveBeenCalledOnce()
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe(`${ISSUE_RADAR_API}/crew/fabric`)
    expect(parsed.searchParams.get('owner')).toBe('acme')
    expect(parsed.searchParams.get('repo')).toBe('demo-repo')
    // No identity was supplied, so provider/host must NOT ride on the request.
    expect(parsed.searchParams.has('provider')).toBe(false)
    expect(parsed.searchParams.has('host')).toBe(false)
    // GET is the default (no method / body set); credentials are same-origin.
    expect(init.method ?? 'GET').toBe('GET')
    expect(init.credentials).toBe('same-origin')
  })

  it('carries provider and host on the request when the ref has an identity', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ schema: 1, phases: [], items: [] }))

    await autoTriagePipelineApi.crewFabric({
      owner: 'grp',
      repo: 'proj',
      provider: 'gitlab',
      host: 'gitlab.example.com',
    })

    const parsed = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(parsed.searchParams.get('provider')).toBe('gitlab')
    expect(parsed.searchParams.get('host')).toBe('gitlab.example.com')
  })

  it('returns the folded payload verbatim when the response is a well-formed 200', async () => {
    const wire: CrewFabricResponse = {
      schema: 1,
      owner: 'acme',
      repo: 'demo-repo',
      provider: 'github',
      host: null,
      generated_at: '2026-08-24T00:00:00Z',
      phases: ['selected', 'resolved'],
      items: [
        {
          number: 42,
          crew_id: 'crew-1',
          title: 'Fix the thing',
          next: 'add the branch',
          pr_number: 100,
          phase: 'implementing',
          timeline: [{ phase: 'selected', at: '2026-08-24T00:00:00Z' }],
        },
      ],
    }
    fetchSpy.mockResolvedValue(jsonResponse(wire))

    const res = await autoTriagePipelineApi.crewFabric({ owner: 'acme', repo: 'demo-repo' })

    expect(res.schema).toBe(1)
    expect(res.generated_at).toBe('2026-08-24T00:00:00Z')
    expect(res.phases).toEqual(['selected', 'resolved'])
    expect(res.items).toHaveLength(1)
    expect(res.items[0].number).toBe(42)
    expect(res.items[0].phase).toBe('implementing')
  })

  it('degrades to a normalized empty result (items: [], HTTP 200 shape) for a repo with no crews', async () => {
    // The documented COMMON case: a valid 200 whose items array is empty.
    fetchSpy.mockResolvedValue(
      jsonResponse({ schema: 1, owner: 'o', repo: 'r', phases: [], items: [] }),
    )

    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })

    expect(res.items).toEqual([])
    // An empty phases array from the server is replaced by the full enum so a
    // drawing always has its columns.
    expect(res.phases).toEqual([...CREW_PHASES])
  })

  it('never throws on a non-2xx response — synthesizes the empty result carrying the requested ref', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ error: 'not found' }, 404))

    const ref: RepoRef = { owner: 'o', repo: 'r', provider: 'github', host: 'github.com' }
    const res = await autoTriagePipelineApi.crewFabric(ref)

    expect(res.schema).toBe(CREW_FABRIC_SCHEMA)
    expect(res.owner).toBe('o')
    expect(res.repo).toBe('r')
    expect(res.provider).toBe('github')
    expect(res.host).toBe('github.com')
    expect(res.generated_at).toBeNull()
    expect(res.phases).toEqual([...CREW_PHASES])
    expect(res.items).toEqual([])
  })

  it('synthesizes the empty result for a 500', async () => {
    fetchSpy.mockResolvedValue(rawResponse('internal error', 500))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })
    expect(res.items).toEqual([])
    expect(res.host).toBeNull()
  })

  it('synthesizes the empty result for a malformed / non-JSON body at 200', async () => {
    fetchSpy.mockResolvedValue(rawResponse('<html>not json</html>', 200))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })
    expect(res.items).toEqual([])
    expect(res.phases).toEqual([...CREW_PHASES])
  })

  it('synthesizes the empty result when the JSON body is not an object', async () => {
    fetchSpy.mockResolvedValue(jsonResponse(42))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })
    expect(res.items).toEqual([])
  })

  it('synthesizes the empty result when the payload lacks an items array (newer/wrong schema)', async () => {
    // A payload from a newer schema that no longer carries `items` as an array
    // must not crash the read — it collapses to empty.
    fetchSpy.mockResolvedValue(jsonResponse({ schema: 2, items: { not: 'an array' } }))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })
    expect(res.items).toEqual([])
  })

  it('never throws on a transport-level failure (offline / DNS)', async () => {
    fetchSpy.mockRejectedValue(new TypeError('Failed to fetch'))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })
    expect(res.items).toEqual([])
    expect(res.owner).toBe('o')
  })

  it('preserves an explicit schema number and falls back to the ref for missing owner/repo', async () => {
    // items present but owner/repo/schema partial: the client fills owner/repo
    // from the ref and keeps the server schema when it is a number.
    fetchSpy.mockResolvedValue(jsonResponse({ schema: 7, items: [] }))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'fallback-o', repo: 'fallback-r' })
    expect(res.schema).toBe(7)
    expect(res.owner).toBe('fallback-o')
    expect(res.repo).toBe('fallback-r')
  })

  it('defaults schema to CREW_FABRIC_SCHEMA when the body omits a numeric schema', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ items: [] }))
    const res = await autoTriagePipelineApi.crewFabric({ owner: 'o', repo: 'r' })
    expect(res.schema).toBe(CREW_FABRIC_SCHEMA)
  })
})

describe('autoTriagePipelineApi.listConnectedRepos', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('GETs the /repos endpoint with same-origin credentials', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ repos: [] }))
    await autoTriagePipelineApi.listConnectedRepos()
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${ISSUE_RADAR_API}/repos`)
    expect(init.credentials).toBe('same-origin')
  })

  it('returns the coerced connected-repo list on a well-formed 200', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        repos: [
          { owner: 'a', repo: 'x', provider: 'gitlab', host: 'gitlab.com', enabled: true },
          { owner: 'b', repo: 'y' },
        ],
      }),
    )
    const repos = await autoTriagePipelineApi.listConnectedRepos()
    expect(repos).toHaveLength(2)
    expect(repos[0]).toEqual({
      owner: 'a',
      repo: 'x',
      provider: 'gitlab',
      host: 'gitlab.com',
      enabled: true,
    })
    // A legacy record without provider/host/enabled keeps only owner/repo.
    expect(repos[1]).toEqual({ owner: 'b', repo: 'y' })
  })

  it('skips malformed rows (non-object, missing/empty owner or repo, wrong field types)', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        repos: [
          null,
          'nope',
          { owner: 'ok', repo: 'good' },
          { owner: '', repo: 'r' },
          { owner: 'o', repo: '' },
          { owner: 5, repo: 'r' },
          { owner: 'o', repo: 'r', provider: 9, host: 10, enabled: 'yes' },
        ],
      }),
    )
    const repos = await autoTriagePipelineApi.listConnectedRepos()
    // Only the fully-valid row and the last row (kept, but with the non-boolean
    // enabled / non-string provider+host dropped) survive.
    expect(repos).toEqual([
      { owner: 'ok', repo: 'good' },
      { owner: 'o', repo: 'r' },
    ])
  })

  it('returns [] on a non-2xx response', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ error: 'disabled' }, 403))
    await expect(autoTriagePipelineApi.listConnectedRepos()).resolves.toEqual([])
  })

  it('returns [] on a non-JSON body', async () => {
    fetchSpy.mockResolvedValue(rawResponse('<html>502</html>', 502))
    await expect(autoTriagePipelineApi.listConnectedRepos()).resolves.toEqual([])
  })

  it('returns [] when the JSON body is not an object', async () => {
    fetchSpy.mockResolvedValue(jsonResponse('a string'))
    await expect(autoTriagePipelineApi.listConnectedRepos()).resolves.toEqual([])
  })

  it('returns [] when the payload has no repos array', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ repos: 'not-an-array' }))
    await expect(autoTriagePipelineApi.listConnectedRepos()).resolves.toEqual([])
  })

  it('returns [] on a transport-level failure', async () => {
    fetchSpy.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(autoTriagePipelineApi.listConnectedRepos()).resolves.toEqual([])
  })
})

describe('repo preference storage', () => {
  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('loadStoredPreference returns null when neither key is set', () => {
    expect(loadStoredPreference()).toBeNull()
  })

  it('saveRepoPreference persists only owner/repo when no identity is given, and reloads it', () => {
    saveRepoPreference({ owner: 'o', repo: 'r' })
    const stored = JSON.parse(localStorage.getItem(REPO_PREFERENCE_KEY) as string)
    expect(stored).toEqual({ owner: 'o', repo: 'r' })
    expect(loadStoredPreference()).toEqual({ owner: 'o', repo: 'r' })
  })

  it('saveRepoPreference persists provider and host when present', () => {
    saveRepoPreference({ owner: 'o', repo: 'r', provider: 'gitlab', host: 'gl.example.com' })
    expect(loadStoredPreference()).toEqual({
      owner: 'o',
      repo: 'r',
      provider: 'gitlab',
      host: 'gl.example.com',
    })
  })

  it("prefers this app's own key over Issue Radar's active-repo key", () => {
    localStorage.setItem(
      ISSUE_RADAR_ACTIVE_REPO_KEY,
      JSON.stringify({ owner: 'radar', repo: 'from-radar' }),
    )
    localStorage.setItem(REPO_PREFERENCE_KEY, JSON.stringify({ owner: 'own', repo: 'from-own' }))
    expect(loadStoredPreference()).toEqual({ owner: 'own', repo: 'from-own' })
  })

  it("falls back to Issue Radar's active-repo key on a first-ever visit here", () => {
    localStorage.setItem(
      ISSUE_RADAR_ACTIVE_REPO_KEY,
      JSON.stringify({ owner: 'radar', repo: 'seed', provider: 'github' }),
    )
    expect(loadStoredPreference()).toEqual({ owner: 'radar', repo: 'seed', provider: 'github' })
  })

  it('discards a malformed stored value (bad JSON) rather than throwing', () => {
    localStorage.setItem(REPO_PREFERENCE_KEY, '{not valid json')
    expect(loadStoredPreference()).toBeNull()
  })

  it('discards a stored value missing owner/repo, and one that is not an object', () => {
    localStorage.setItem(REPO_PREFERENCE_KEY, JSON.stringify({ owner: 'o' }))
    expect(loadStoredPreference()).toBeNull()
    localStorage.setItem(REPO_PREFERENCE_KEY, JSON.stringify(['array']))
    expect(loadStoredPreference()).toBeNull()
    localStorage.setItem(REPO_PREFERENCE_KEY, JSON.stringify(null))
    expect(loadStoredPreference()).toBeNull()
  })

  it('drops non-string provider/host while keeping a valid owner/repo', () => {
    localStorage.setItem(
      REPO_PREFERENCE_KEY,
      JSON.stringify({ owner: 'o', repo: 'r', provider: 5, host: {} }),
    )
    expect(loadStoredPreference()).toEqual({ owner: 'o', repo: 'r' })
  })

  it('saveRepoPreference swallows a storage failure (private mode / quota)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    // Must not throw.
    expect(() => saveRepoPreference({ owner: 'o', repo: 'r' })).not.toThrow()
  })
})
