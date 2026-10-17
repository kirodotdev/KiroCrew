/**
 * Tests for the Auto Triage Pipeline API client (`./api`).
 *
 * The module reads THROUGH Issue Radar's crew-fabric seam and PROMISES to be
 * forward-tolerant: `crewFabric` and the fold clients never throw on "no
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
  CREW_PHASES,
  CREW_FABRIC_SCHEMA,
  isQueueMigrationPending,
  isUnsupportedForge,
  type ApiError,
  type CrewFabricResponse,
  type RepoRef,
} from './api'

const ISSUE_RADAR_API = '/api/apps/issue-radar'
const PIPELINE_API = '/api/apps/issue-radar/pipeline'

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
    await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe(`${PIPELINE_API}/overview`)
    expect(parsed.searchParams.has('hours')).toBe(false)
    expect((init.method ?? 'GET')).toBe('GET')
    expect(init.credentials).toBe('same-origin')
  })

  it('passes an integer hours param, truncating a fractional value', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [] }))
    await autoTriagePipelineFoldApi.overview(48.9, { owner: 'acme', repo: 'alpha' })
    const parsed = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(parsed.searchParams.get('hours')).toBe('48')
  })

  it('always sends the repository, whether or not hours is given', async () => {
    // The repository is required, and the backend refuses a bare request with 400
    // `repo_required` -- so an optional client parameter could only build a request
    // that cannot succeed. What this pins is the property that survives that: the
    // repository rides on the request INDEPENDENTLY of `hours`, so it cannot be lost
    // on whichever call happens to build its query string differently.
    const repo = { owner: 'acme', repo: 'alpha' } as const

    fetchSpy.mockResolvedValue(jsonResponse({ steps: [] }))
    await autoTriagePipelineFoldApi.overview(undefined, repo)
    const noHours = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(noHours.searchParams.get('owner')).toBe('acme')
    expect(noHours.searchParams.get('repo')).toBe('alpha')
    expect(noHours.searchParams.has('hours')).toBe(false)

    // Cleared first: `calledUrl` reads the FIRST recorded call, so without this the
    // assertion below re-inspects the request above and passes for the wrong reason.
    fetchSpy.mockClear()
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [] }))
    await autoTriagePipelineFoldApi.overview(12, repo)
    const withHours = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(withHours.searchParams.get('hours')).toBe('12')
    expect(withHours.searchParams.get('owner')).toBe('acme')
    expect(withHours.searchParams.get('repo')).toBe('alpha')
  })

  it('sends the WHOLE identity when given a repository, not just the slug', async () => {
    // owner/repo alone is not a repository, and the backend refuses anything that
    // is not public GitHub — which it can only do if the request says which forge
    // it means. A bare pair would claim "public GitHub" on the caller's behalf and
    // read that repository's events, sessions and credit costs under another's
    // name. Pinned because both of this branch's repo-scoped calls originally sent
    // the pair alone.
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [] }))
    await autoTriagePipelineFoldApi.overview(undefined, {
      owner: 'acme', repo: 'alpha', provider: 'gitlab', host: 'gitlab.example.com',
    })
    const url = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(url.searchParams.get('owner')).toBe('acme')
    expect(url.searchParams.get('repo')).toBe('alpha')
    expect(url.searchParams.get('provider')).toBe('gitlab')
    expect(url.searchParams.get('host')).toBe('gitlab.example.com')
  })

  it('omits provider and host when the repository does not carry them', async () => {
    // Absent means public GitHub everywhere in Issue Radar. Sending an invented
    // `provider=github` would be harmless today but makes the request assert
    // something the caller never said, and the backend already has that default.
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [] }))
    await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
    const url = new URL(calledUrl(fetchSpy), 'http://localhost')
    expect(url.searchParams.get('owner')).toBe('acme')
    expect(url.searchParams.has('provider')).toBe(false)
    expect(url.searchParams.has('host')).toBe(false)
  })

  it('coerces the repository census and drops a row with no name', async () => {
    // A nameless row is what `unattributedEvents` already counts. Kept as an empty
    // label it would render a blank entry that matches nothing when selected.
    fetchSpy.mockResolvedValue(
      jsonResponse({
        steps: [],
        unattributedEvents: 4565,
        repos: [
          { repo: 'acme/alpha', count: 208 },
          { repo: '', count: 9 },
          { count: 3 },
          'not-an-object',
        ],
      }),
    )
    const res = await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
    expect(res.unattributedEvents).toBe(4565)
    expect(res.repos).toEqual([{ repo: 'acme/alpha', count: 208 }])
  })

  it('defaults the new fields when the server omits them', async () => {
    // An older gateway does not send them; the view must read 0 and [] rather than
    // undefined, or a count would render as NaN.
    fetchSpy.mockResolvedValue(jsonResponse({ steps: [], totalEvents: 7 }))
    const res = await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
    expect(res.unattributedEvents).toBe(0)
    expect(res.repos).toEqual([])
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
    const res = await autoTriagePipelineFoldApi.overview(24, { owner: 'acme', repo: 'alpha' })
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
    const res = await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
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
    const res = await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
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
    await expect(autoTriagePipelineFoldApi.overview(12, { owner: 'acme', repo: 'alpha' })).rejects.toThrow(/503/)
  })

  it('THROWS on a non-JSON body, a non-object body, and a transport failure', async () => {
    fetchSpy.mockResolvedValueOnce(rawResponse('<html>500</html>', 500))
    await expect(autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })).rejects.toThrow()
    fetchSpy.mockResolvedValueOnce(jsonResponse(42))
    await expect(autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })).rejects.toThrow()
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })).rejects.toThrow()
  })

  it('returns empty steps when the payload steps field is not an array', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ steps: { not: 'array' }, totalEvents: 9 }))
    const res = await autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' })
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
    expect(parsed.pathname).toBe(`${PIPELINE_API}/step`)
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
    await autoTriagePipelineFoldApi.itemSessions(42.9, { owner: 'acme', repo: 'alpha' })
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe(`${PIPELINE_API}/item/sessions`)
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
    const res = await autoTriagePipelineFoldApi.itemSessions(5600, { owner: 'acme', repo: 'alpha' })
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
    const res = await autoTriagePipelineFoldApi.itemSessions(7, { owner: 'acme', repo: 'alpha' })
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
    const res = await autoTriagePipelineFoldApi.itemSessions(1, { owner: 'acme', repo: 'alpha' })
    expect(res.sessions[0].credits).toBe(0)
    expect(res.sessions[0].durationMs).toBe(0)
  })

  it('reads a non-array sessions as empty and echoes the requested number', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ number: 9, sessions: { not: 'array' } }))
    const res = await autoTriagePipelineFoldApi.itemSessions(9, { owner: 'acme', repo: 'alpha' })
    expect(res.sessions).toEqual([])
    expect(res.number).toBe(9)
  })

  it('THROWS on every degraded path rather than reporting no sessions', async () => {
    // "This item never opened a session" is a claim about the pipeline; a failed
    // request is a claim about the request. Conflating them told the operator the
    // work never happened.
    fetchSpy.mockResolvedValueOnce(jsonResponse({ error: 'bad_item' }, 400))
    await expect(autoTriagePipelineFoldApi.itemSessions(11, { owner: 'acme', repo: 'alpha' })).rejects.toThrow(/400/)
    fetchSpy.mockResolvedValueOnce(rawResponse('<html>503</html>', 503))
    await expect(autoTriagePipelineFoldApi.itemSessions(11, { owner: 'acme', repo: 'alpha' })).rejects.toThrow()
    fetchSpy.mockResolvedValueOnce(jsonResponse(42))
    await expect(autoTriagePipelineFoldApi.itemSessions(11, { owner: 'acme', repo: 'alpha' })).rejects.toThrow()
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(autoTriagePipelineFoldApi.itemSessions(11, { owner: 'acme', repo: 'alpha' })).rejects.toThrow()
  })

  it('falls back to the requested number when the body omits it', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ count: 0, sessions: [], populatedColumns: [] }))
    const res = await autoTriagePipelineFoldApi.itemSessions(88, { owner: 'acme', repo: 'alpha' })
    expect(res.number).toBe(88)
  })
})

describe('the refusal code crossing from the HTTP body onto the error', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })
  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('lifts the backend code off a refusal so a view can recognise it', async () => {
    // A view tells a refused forge apart from a failed read by this code, and this is
    // the ONLY place it crosses from the response body onto the error. Asserted here
    // because a view test builds its error object directly, so it stays green with
    // this plumbing removed -- leaving the explanatory band silently unreachable.
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ error: 'nope', code: 'repo_provider_unsupported' }),
    } as unknown as Response)

    await expect(
      autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' }),
    ).rejects.toMatchObject({ code: 'repo_provider_unsupported', status: 400 })
  })

  it('does not invent a code when the error body carries none', async () => {
    // A plain failure must stay one: recognising it as a refused forge would tell an
    // operator this board cannot read the repository when the backend is merely down.
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ error: 'unreadable' }),
    } as unknown as Response)

    await expect(
      autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' }),
    ).rejects.not.toMatchObject({ code: 'repo_provider_unsupported' })
  })

  it('still throws usefully when the error body is not JSON', async () => {
    // A proxy returning HTML is a real case, and parsing it must not replace the
    // status-bearing throw with a parse error from the error path itself.
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response)

    await expect(
      autoTriagePipelineFoldApi.overview(undefined, { owner: 'acme', repo: 'alpha' }),
    ).rejects.toThrow(/502/)
  })
})


describe('isQueueMigrationPending', () => {
  // RECOGNITION of the backend's `queue_migration_pending` refusal (raised by
  // `_read_queue`, answered 503 by `_handle_step` / `_handle_item_sessions`). The
  // view keys its migration-specific copy on this, so the code must be recognised
  // by its exact value and nothing else.
  it('is true only for an error carrying code queue_migration_pending', () => {
    const err = new Error('migrate') as ApiError
    err.code = 'queue_migration_pending'
    expect(isQueueMigrationPending(err)).toBe(true)
  })

  it('is false for a different code, a codeless error, and non-errors', () => {
    const other = new Error('x') as ApiError
    other.code = 'repo_provider_unsupported'
    expect(isQueueMigrationPending(other)).toBe(false)
    expect(isQueueMigrationPending(new Error('plain'))).toBe(false)
    expect(isQueueMigrationPending(null)).toBe(false)
    expect(isQueueMigrationPending(undefined)).toBe(false)
    // The two recognizers must not answer to each other's code, or the view would
    // show the wrong band for the wrong refusal.
    expect(isUnsupportedForge(err_qmp())).toBe(false)
  })

  it('lifts the code off a real 503 refusal so the recognizer fires end-to-end', async () => {
    // The whole path: the fold client lifts the body `code` onto the error (the only
    // place it crosses), and the recognizer reads it. A view test builds its error
    // object directly, so without this the plumbing could be removed and the view
    // test would stay green while the migration band went unreachable in the product.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        error: 'The dispatch queue has not been sharded per repository yet; run the pipeline installer to migrate it',
        code: 'queue_migration_pending',
      }),
    } as unknown as Response)
    try {
      await autoTriagePipelineFoldApi.step({ step: 'implement', owner: 'acme', repo: 'alpha' })
      throw new Error('expected the refusal to throw')
    } catch (err) {
      expect(isQueueMigrationPending(err)).toBe(true)
    } finally {
      fetchSpy.mockRestore()
    }
  })
})

/** A queue-migration-pending error, built the way a view test builds one. */
function err_qmp(): ApiError {
  const e = new Error('migrate') as ApiError
  e.code = 'queue_migration_pending'
  return e
}
