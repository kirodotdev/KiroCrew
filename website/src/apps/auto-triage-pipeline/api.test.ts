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
  autoTriagePipelineFoldApi,
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
const ATP_API = '/api/apps/auto-triage-pipeline'

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

// ---------------------------------------------------------------------------
// The app's OWN backend clients — overview / step / itemSessions. Same
// forward-tolerant law as crewFabric: assert the request built AND the coerced
// shape on the well-formed path and on every degraded path (transport failure,
// non-2xx, non-JSON, non-object, partial/newer-schema payload).
// ---------------------------------------------------------------------------

describe('autoTriagePipelineFoldApi.overview', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })
  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('GETs /overview with no hours param when none is given, same-origin credentials', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [], totalEvents: 0 }))
    await autoTriagePipelineFoldApi.overview()
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe(`${ATP_API}/overview`)
    expect(parsed.searchParams.has('hours')).toBe(false)
    expect((init.method ?? 'GET')).toBe('GET')
    expect(init.credentials).toBe('same-origin')
  })

  it('passes an integer hours param, truncating a fractional value', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [] }))
    await autoTriagePipelineFoldApi.overview(48.9)
    const parsed = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(parsed.searchParams.get('hours')).toBe('48')
  })

  it('coerces a well-formed overview payload, including routed and unmapped arrays', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        steps: [
          {
            key: 'triage',
            label: 'Triage',
            unit: 'issues',
            entered: 392,
            done: 102,
            skipped: 0,
            churn: 0,
            recentEntered: 5,
            recentDone: 2,
            inFlight: 3,
            distinctEntered: 300,
            distinctDone: 100,
            routed: [
              { outcome: 'auto-fixable', count: 102 },
              { outcome: 'needs-human', count: 290 },
            ],
          },
          {
            key: 'implement',
            label: 'Implement',
            unit: 'sessions',
            entered: 197,
            done: 83,
            skipped: 4,
            churn: 0,
            recentEntered: 1,
            recentDone: 1,
            inFlight: 2,
            distinctEntered: 113,
            distinctDone: 83,
            routed: [],
          },
        ],
        totalEvents: 1234,
        unparseable: 2,
        unmappedEvents: [{ event: 'weird_event', count: 3 }],
        firstEventAt: 1_700_000_000,
        lastEventAt: 1_700_100_000,
        recentHours: 24,
      }),
    )
    const res = await autoTriagePipelineFoldApi.overview(24)
    expect(res.steps).toHaveLength(2)
    // `done` legitimately below `entered` here (event counts), and unit differs.
    expect(res.steps[0].unit).toBe('issues')
    expect(res.steps[0].entered).toBe(392)
    expect(res.steps[0].routed).toEqual([
      { outcome: 'auto-fixable', count: 102 },
      { outcome: 'needs-human', count: 290 },
    ])
    expect(res.steps[1].unit).toBe('sessions')
    expect(res.totalEvents).toBe(1234)
    expect(res.unparseable).toBe(2)
    expect(res.unmappedEvents).toEqual([{ event: 'weird_event', count: 3 }])
    expect(res.firstEventAt).toBe(1_700_000_000)
    expect(res.lastEventAt).toBe(1_700_100_000)
    expect(res.recentHours).toBe(24)
  })

  it('defaults an unknown unit to issues and drops malformed step/routed/unmapped entries', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        steps: [
          null,
          'nope',
          { key: 'scan', label: 'Scan', unit: 'batch', routed: [null, 'x', { outcome: 'ok', count: 1 }] },
        ],
        unmappedEvents: [null, { event: 'e', count: 2 }, 'bad'],
      }),
    )
    const res = await autoTriagePipelineFoldApi.overview()
    expect(res.steps).toHaveLength(1)
    // 'batch' is not a known unit -> defaults to 'issues'.
    expect(res.steps[0].unit).toBe('issues')
    // Missing numeric fields coerce to 0.
    expect(res.steps[0].entered).toBe(0)
    expect(res.steps[0].routed).toEqual([{ outcome: 'ok', count: 1 }])
    expect(res.unmappedEvents).toEqual([{ event: 'e', count: 2 }])
  })

  it('treats timestamps as epoch seconds that may be null', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ steps: [], firstEventAt: null, lastEventAt: 'not-a-number' }),
    )
    const res = await autoTriagePipelineFoldApi.overview()
    expect(res.firstEventAt).toBeNull()
    // A non-numeric timestamp coerces to null, not 0.
    expect(res.lastEventAt).toBeNull()
  })

  it('THROWS on a non-2xx rather than reporting an empty pipeline', async () => {
    // The whole point: a request failure must reach the query so the view can say
    // "could not load" instead of "No pipeline activity yet". Returning an empty
    // payload here made the views' error branches unreachable and put a confident
    // false fact in front of a backend outage.
    fetchSpy.mockResolvedValue(jsonResponse({ error: 'unreadable' }, 503))
    await expect(autoTriagePipelineFoldApi.overview(12)).rejects.toThrow(/503/)
  })

  it('THROWS on a non-JSON body, a non-object body, and a transport failure', async () => {
    fetchSpy.mockResolvedValueOnce(rawResponse('<html>500</html>', 500))
    await expect(autoTriagePipelineFoldApi.overview()).rejects.toThrow()
    fetchSpy.mockResolvedValueOnce(jsonResponse(42))
    await expect(autoTriagePipelineFoldApi.overview()).rejects.toThrow()
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(autoTriagePipelineFoldApi.overview()).rejects.toThrow()
  })

  it('returns empty steps when the payload steps field is not an array', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ steps: { not: 'array' }, totalEvents: 9 }))
    const res = await autoTriagePipelineFoldApi.overview()
    expect(res.steps).toEqual([])
    // Scalar fields present alongside a bad steps array are still read.
    expect(res.totalEvents).toBe(9)
  })
})

describe('autoTriagePipelineFoldApi.step', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })
  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('GETs /step with step/owner/repo, omitting provider/host/limit when absent', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ step: 'implement', count: 0, items: [] }))
    await autoTriagePipelineFoldApi.step({ step: 'implement', owner: 'acme', repo: 'demo' })
    const parsed = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(parsed.pathname).toBe(`${ATP_API}/step`)
    expect(parsed.searchParams.get('step')).toBe('implement')
    expect(parsed.searchParams.get('owner')).toBe('acme')
    expect(parsed.searchParams.get('repo')).toBe('demo')
    expect(parsed.searchParams.has('provider')).toBe(false)
    expect(parsed.searchParams.has('host')).toBe(false)
    expect(parsed.searchParams.has('limit')).toBe(false)
  })

  it('carries provider, host and a truncated integer limit when supplied', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ step: 's', count: 0, items: [] }))
    await autoTriagePipelineFoldApi.step({
      step: 'verify',
      owner: 'grp',
      repo: 'proj',
      provider: 'gitlab',
      host: 'gitlab.example.com',
      limit: 50.7,
    })
    const parsed = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(parsed.searchParams.get('provider')).toBe('gitlab')
    expect(parsed.searchParams.get('host')).toBe('gitlab.example.com')
    expect(parsed.searchParams.get('limit')).toBe('50')
  })

  it('coerces a well-formed step payload, including a nested events trail', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        step: 'implement',
        count: 1,
        items: [
          {
            number: 5600,
            title: 'Fix _safe_chmod on Windows',
            labels: ['bug', 'auto-fixable'],
            author: 'octocat',
            assignees: ['maintainer'],
            comments: 3,
            queuedAt: 1_700_000_000,
            dispatchedAt: 1_700_000_100,
            resumeCount: 2,
            slot: 'slot-c',
            previousSlots: ['slot-a', 'slot-b'],
            withdrawn: false,
            needsHuman: false,
            pr: 5601,
            lastEvent: 'pr_opened',
            lastEventAt: 1_700_000_500,
            events: [
              { event: 'implement_start', ts: 1_700_000_100 },
              { event: 'pr_opened', ts: 1_700_000_500 },
            ],
          },
        ],
      }),
    )
    const res = await autoTriagePipelineFoldApi.step({ step: 'implement', owner: 'o', repo: 'r' })
    expect(res.step).toBe('implement')
    expect(res.count).toBe(1)
    expect(res.items).toHaveLength(1)
    const item = res.items[0]
    expect(item.number).toBe(5600)
    expect(item.labels).toEqual(['bug', 'auto-fixable'])
    expect(item.previousSlots).toEqual(['slot-a', 'slot-b'])
    expect(item.pr).toBe(5601)
    // `events` is NOT surfaced. The expanded row's trail strip was removed, so the
    // field shipped with no renderer -- up to 200 events across up to 2000 items per
    // response. Pinned as absent so it cannot quietly return without a consumer.
    expect('events' in item).toBe(false)
  })

  it('degrades every absent field on a partial item rather than throwing', async () => {
    // A live-log item with only its number and a null-timestamp event.
    fetchSpy.mockResolvedValue(
      jsonResponse({
        step: 'scan',
        count: 1,
        items: [{ number: 1, events: [{ event: 'scan', ts: null }, null, 'bad'] }],
      }),
    )
    const res = await autoTriagePipelineFoldApi.step({ step: 'scan', owner: 'o', repo: 'r' })
    const item = res.items[0]
    expect(item.number).toBe(1)
    expect(item.title).toBe('')
    expect(item.labels).toEqual([])
    expect(item.assignees).toEqual([])
    // NULL, not 0. An absent comment count means the local issue cache has no
    // answer, which is a different fact from "this issue has no comments" -- the
    // same distinction its neighbouring labels/assignees already make by rendering
    // "Not cached". Degrading it to 0 made the row assert something the data did
    // not say.
    expect(item.comments).toBeNull()
    expect(item.queuedAt).toBeNull()
    expect(item.dispatchedAt).toBeNull()
    expect(item.pr).toBeNull()
    expect(item.withdrawn).toBe(false)
    expect(item.needsHuman).toBe(false)
  })

  it('drops malformed item rows and reads a non-array items as empty', async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ step: 'x', count: 2, items: [null, 'nope', { number: 7 }] }),
    )
    const res1 = await autoTriagePipelineFoldApi.step({ step: 'x', owner: 'o', repo: 'r' })
    expect(res1.items).toHaveLength(1)
    expect(res1.items[0].number).toBe(7)

    fetchSpy.mockResolvedValueOnce(jsonResponse({ step: 'x', items: { not: 'array' } }))
    const res2 = await autoTriagePipelineFoldApi.step({ step: 'x', owner: 'o', repo: 'r' })
    expect(res2.items).toEqual([])
  })

  it('THROWS on every degraded path rather than reporting an empty step', async () => {
    // "No items in this step" and "we could not ask" are different facts, and only
    // one of them is safe to render as a heading.
    const call = () => autoTriagePipelineFoldApi.step({ step: 'ghost', owner: 'o', repo: 'r' })
    fetchSpy.mockResolvedValueOnce(jsonResponse({ error: 'bad_step' }, 400))
    await expect(call()).rejects.toThrow(/400/)
    fetchSpy.mockResolvedValueOnce(rawResponse('<html>503</html>', 503))
    await expect(call()).rejects.toThrow()
    fetchSpy.mockResolvedValueOnce(jsonResponse('a string'))
    await expect(call()).rejects.toThrow()
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(call()).rejects.toThrow()
  })

  it('falls back to the requested step when the body omits it', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ count: 0, items: [] }))
    const res = await autoTriagePipelineFoldApi.step({ step: 'verify', owner: 'o', repo: 'r' })
    expect(res.step).toBe('verify')
  })
})

describe('autoTriagePipelineFoldApi.itemSessions', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })
  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('GETs /item/sessions with a truncated integer number and same-origin credentials', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ number: 42, count: 0, sessions: [], populatedColumns: [] }))
    await autoTriagePipelineFoldApi.itemSessions(42.9)
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe(`${ATP_API}/item/sessions`)
    expect(parsed.searchParams.get('number')).toBe('42')
    expect(init.credentials).toBe('same-origin')
  })

  it('coerces a well-formed sessions payload and preserves populatedColumns', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        number: 5600,
        count: 2,
        sessions: [
          {
            slot: 'slot-c',
            model: 'sonnet',
            agent: 'kirocrew',
            surface: 'cron',
            current: true,
            startedAt: 1_700_000_100,
            lastAt: 1_700_000_900,
            turns: 74,
            input: 0,
            output: 0,
            cacheCreate: 0,
            cacheRead: 0,
            cost: 0,
            credits: 187.5,
            durationMs: 800_000,
            contextUsed: 120_000,
            contextWindow: 200_000,
            lastPhase: 'awaiting-ci',
            lastStopReason: 'end_turn',
          },
          {
            slot: 'slot-a',
            model: 'sonnet',
            current: false,
            turns: 40,
            credits: 3872.15,
          },
        ],
        // tokens and cost are always zero today, so only credit/time columns are
        // populated — the view must render exactly these.
        populatedColumns: ['credits', 'durationMs', 'contextUsed'],
      }),
    )
    const res = await autoTriagePipelineFoldApi.itemSessions(5600)
    expect(res.number).toBe(5600)
    expect(res.count).toBe(2)
    expect(res.sessions).toHaveLength(2)
    // `turns` IS the row count -- the usage endpoint sends one row per turn, and
    // the backend does not ship the rows' own structurally-zero `turns` field at
    // all, so there is no near-identical key a consumer could render by mistake.
    expect(res.sessions[0].turns).toBe(74)
    expect('rows' in res.sessions[0]).toBe(false)
    expect('rawTurns' in res.sessions[0]).toBe(false)
    expect(res.sessions[0].credits).toBe(187.5)
    expect(res.sessions[1].credits).toBe(3872.15)
    expect(res.populatedColumns).toEqual(['credits', 'durationMs', 'contextUsed'])
  })

  it('degrades absent fields on a partial session and drops malformed rows', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        number: 7,
        sessions: [null, 'nope', { slot: 'only-slot' }],
      }),
    )
    const res = await autoTriagePipelineFoldApi.itemSessions(7)
    expect(res.sessions).toHaveLength(1)
    const s = res.sessions[0]
    expect(s.slot).toBe('only-slot')
    expect(s.model).toBe('')
    expect(s.current).toBe(false)
    expect(s.startedAt).toBeNull()
    expect(s.lastAt).toBeNull()
    expect(s.turns).toBe(0)
    expect(s.credits).toBe(0)
    // Absent populatedColumns coerces to [].
    expect(res.populatedColumns).toEqual([])
  })

  it('coerces a NaN/Infinity numeric field to 0 rather than propagating it', async () => {
    // JSON cannot carry NaN, but a hand-built object can reach the coercer; assert
    // the guard via a stringy field which must also coerce to 0.
    fetchSpy.mockResolvedValue(
      jsonResponse({ number: 1, sessions: [{ slot: 's', credits: 'lots', durationMs: null }] }),
    )
    const res = await autoTriagePipelineFoldApi.itemSessions(1)
    expect(res.sessions[0].credits).toBe(0)
    expect(res.sessions[0].durationMs).toBe(0)
  })

  it('reads a non-array sessions as empty and echoes the requested number', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ number: 9, sessions: { not: 'array' } }))
    const res = await autoTriagePipelineFoldApi.itemSessions(9)
    expect(res.sessions).toEqual([])
    expect(res.number).toBe(9)
  })

  it('THROWS on every degraded path rather than reporting no sessions', async () => {
    // "This item never opened a session" is a claim about the pipeline; a failed
    // request is a claim about the request. Conflating them told the operator the
    // work never happened.
    fetchSpy.mockResolvedValueOnce(jsonResponse({ error: 'bad_item' }, 400))
    await expect(autoTriagePipelineFoldApi.itemSessions(11)).rejects.toThrow(/400/)
    fetchSpy.mockResolvedValueOnce(rawResponse('<html>503</html>', 503))
    await expect(autoTriagePipelineFoldApi.itemSessions(11)).rejects.toThrow()
    fetchSpy.mockResolvedValueOnce(jsonResponse(42))
    await expect(autoTriagePipelineFoldApi.itemSessions(11)).rejects.toThrow()
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(autoTriagePipelineFoldApi.itemSessions(11)).rejects.toThrow()
  })

  it('falls back to the requested number when the body omits it', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ count: 0, sessions: [], populatedColumns: [] }))
    const res = await autoTriagePipelineFoldApi.itemSessions(88)
    expect(res.number).toBe(88)
  })
})
