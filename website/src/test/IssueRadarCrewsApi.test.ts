import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// The CREWS half of the Issue Radar client, tested at the fetch boundary — the
// component tests mock `issueRadarApi`, so every translation below is invisible to
// them and silent when it breaks: a crew id that never reaches the query resolves
// to "no such crew", a DELETE that degrades to POST retires nothing, and a work
// write that drops `number` overwrites the wrong issue's record.
//
// The phase predicates are here for the same reason. They mirror frozensets in
// `crew_store.py`, and a drift between the two is invisible until a fleet quietly
// stops picking up work because a finished item is still charged to a slot.
import {
  issueRadarApi,
  CREW_PHASES,
  TERMINAL_PHASES,
  TTL_ACTIVE_PHASES,
  EDITING_PHASES,
  countsTowardOpen,
  type CrewPhase,
  type RepoRef,
} from '../apps/issue-radar/api'

const REF: RepoRef = { owner: 'o', repo: 'r' }
/** A self-managed GitLab ref: identity is owner/repo PLUS provider and host. */
const GL: RepoRef = { owner: 'group/sub', repo: 'svc', provider: 'gitlab', host: 'gl.corp' }

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

/** A failure whose body is NOT json — an html error page or an empty 502. */
function brokenBody(status: number): Response {
  return {
    ok: false,
    status,
    json: async () => {
      throw new Error('not json')
    },
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

/** The url of the Nth fetch call. */
const calledUrl = (n = 0): string => String(fetchMock.mock.calls[n][0])
/** The RequestInit of the Nth fetch call. */
const calledInit = (n = 0): RequestInit => fetchMock.mock.calls[n][1] as RequestInit
/** The parsed json body of the Nth fetch call. */
const sentBody = (n = 0): Record<string, unknown> =>
  JSON.parse(String(calledInit(n).body)) as Record<string, unknown>

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, {}))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('crew reads — url and query construction', () => {
  it('lists crews under the app prefix with the repo identity', async () => {
    await issueRadarApi.crews(REF)
    expect(calledUrl()).toBe('/api/apps/issue-radar/crews?owner=o&repo=r')
  })

  it('carries provider and host for a self-managed instance', async () => {
    // `group/sub`+`svc` names a DIFFERENT project on gitlab.com than on gl.corp,
    // so a read that drops the host answers about the wrong repo.
    await issueRadarApi.crews(GL)
    const url = calledUrl()
    expect(url).toContain('provider=gitlab')
    expect(url).toContain('host=gl.corp')
    expect(url).toContain('owner=group%2Fsub')
  })

  it('sends the crew id when reading one crew page', async () => {
    await issueRadarApi.crew(REF, 'c_beef')
    expect(calledUrl()).toBe('/api/apps/issue-radar/crew?owner=o&repo=r&id=c_beef')
  })

  it('reads name suggestions from the crews collection', async () => {
    await issueRadarApi.suggestCrewNames(REF)
    expect(calledUrl()).toBe('/api/apps/issue-radar/crews/names?owner=o&repo=r')
  })

  it('reads protocol settings from the repo-wide route', async () => {
    await issueRadarApi.getCrewSettings(REF)
    expect(calledUrl()).toBe('/api/apps/issue-radar/crews/settings?owner=o&repo=r')
  })

  it('sends no method, body or write header on a read', async () => {
    // A GET carrying `Content-Type: application/json` is the shape a read
    // accidentally copied from a write; it is also how a read becomes a preflight.
    await issueRadarApi.crews(REF)
    const init = calledInit()
    expect(init.method).toBeUndefined()
    expect(init.body).toBeUndefined()
    expect(init.headers).toBeUndefined()
  })
})

describe('crew writes — method, route and body', () => {
  it('creates a crew with POST on the collection, spec flattened onto the ref', async () => {
    await issueRadarApi.createCrew(REF, { name: 'Andromeda', max_open: 2, labels: ['bug'] })
    expect(calledUrl()).toBe('/api/apps/issue-radar/crews')
    expect(calledInit().method).toBe('POST')
    expect(sentBody()).toEqual({
      owner: 'o', repo: 'r', name: 'Andromeda', max_open: 2, labels: ['bug'],
    })
  })

  it('updates a crew with PUT, patch flattened alongside the id', async () => {
    await issueRadarApi.updateCrew(REF, 'c_beef', { unattended: false, model: 'claude-opus-5' })
    expect(calledUrl()).toBe('/api/apps/issue-radar/crew')
    expect(calledInit().method).toBe('PUT')
    expect(sentBody()).toEqual({
      owner: 'o', repo: 'r', id: 'c_beef', unattended: false, model: 'claude-opus-5',
    })
  })

  it('sends an empty patch as a valid no-op write', async () => {
    // The store drops unknown keys and validates the known ones, so `{}` is
    // legal — it must not be turned into a body with a stray undefined.
    await issueRadarApi.updateCrew(REF, 'c_beef', {})
    expect(sentBody()).toEqual({ owner: 'o', repo: 'r', id: 'c_beef' })
  })

  it('retires a crew with DELETE and the id in the BODY', async () => {
    // Retire is DELETE-with-a-body by contract, not a query-param delete like
    // `disconnect`. A method that degrades to POST hits a different handler and
    // retires nothing while resolving as success.
    await issueRadarApi.retireCrew(REF, 'c_beef')
    expect(calledUrl()).toBe('/api/apps/issue-radar/crew')
    expect(calledInit().method).toBe('DELETE')
    expect(sentBody()).toEqual({ owner: 'o', repo: 'r', id: 'c_beef' })
  })

  it('records work with PUT, carrying crew id, issue number and the flat patch', async () => {
    await issueRadarApi.recordCrewWork(REF, 'c_beef', 2187, {
      phase: 'implementing',
      next: 'push the branch',
      pr_number: 42,
      tried_approach: 'patch the caller',
      tried_rejected_because: 'the caller is generated',
      event: 'opened a branch',
      event_kind: 'implement',
    })
    expect(calledUrl()).toBe('/api/apps/issue-radar/crew/work')
    expect(calledInit().method).toBe('PUT')
    expect(sentBody()).toEqual({
      owner: 'o', repo: 'r', crew_id: 'c_beef', number: 2187,
      phase: 'implementing', next: 'push the branch', pr_number: 42,
      tried_approach: 'patch the caller',
      tried_rejected_because: 'the caller is generated',
      event: 'opened a branch', event_kind: 'implement',
    })
  })

  it('keeps an explicit null in a work patch', async () => {
    // `pr_number: null` means "there is no PR any more" and must survive to the
    // wire; dropping it leaves a stale PR number attached to the item.
    await issueRadarApi.recordCrewWork(REF, 'c_beef', 7, { pr_number: null })
    expect(sentBody().pr_number).toBeNull()
  })

  it('pauses with a reason and resumes without one', async () => {
    await issueRadarApi.setCrewPaused(REF, 'c_beef', true, 'main is red')
    expect(calledUrl()).toBe('/api/apps/issue-radar/crew/pause')
    expect(calledInit().method).toBe('POST')
    expect(sentBody()).toEqual({
      owner: 'o', repo: 'r', id: 'c_beef', paused: true, reason: 'main is red',
    })

    fetchMock.mockClear()
    await issueRadarApi.setCrewPaused(REF, 'c_beef', false)
    expect(sentBody()).toEqual({
      owner: 'o', repo: 'r', id: 'c_beef', paused: false, reason: '',
    })
  })

  it('merges protocol settings with PUT on the repo-wide route', async () => {
    await issueRadarApi.putCrewSettings(REF, { claim_ttl_hours: 24 })
    expect(calledUrl()).toBe('/api/apps/issue-radar/crews/settings')
    expect(calledInit().method).toBe('PUT')
    expect(sentBody()).toEqual({ owner: 'o', repo: 'r', settings: { claim_ttl_hours: 24 } })
  })

  it('declares a json content type on every write', async () => {
    const writes: Array<() => Promise<unknown>> = [
      () => issueRadarApi.createCrew(REF, { name: 'Leo' }),
      () => issueRadarApi.updateCrew(REF, 'c_1', { enabled: false }),
      () => issueRadarApi.retireCrew(REF, 'c_1'),
      () => issueRadarApi.recordCrewWork(REF, 'c_1', 1, { phase: 'claimed' }),
      () => issueRadarApi.setCrewPaused(REF, 'c_1', true),
      () => issueRadarApi.putCrewSettings(REF, { commit_trailer: 'Crew: {name}' }),
    ]
    for (const write of writes) {
      fetchMock.mockClear()
      await write()
      expect(calledInit().headers).toEqual({ 'Content-Type': 'application/json' })
    }
  })
})

describe('crew calls carry the session cookie', () => {
  it('sets same-origin credentials on every crew read and write', async () => {
    // The gateway authenticates by cookie. `credentials: 'omit'` (the default for
    // a cross-origin request) turns every one of these into a 401.
    const calls: Array<() => Promise<unknown>> = [
      () => issueRadarApi.crews(REF),
      () => issueRadarApi.crew(REF, 'c_1'),
      () => issueRadarApi.suggestCrewNames(REF),
      () => issueRadarApi.getCrewSettings(REF),
      () => issueRadarApi.createCrew(REF, { name: 'Leo' }),
      () => issueRadarApi.updateCrew(REF, 'c_1', { enabled: false }),
      () => issueRadarApi.retireCrew(REF, 'c_1'),
      () => issueRadarApi.recordCrewWork(REF, 'c_1', 1, { phase: 'claimed' }),
      () => issueRadarApi.setCrewPaused(REF, 'c_1', true),
      () => issueRadarApi.putCrewSettings(REF, { claim_ttl_hours: 48 }),
    ]
    for (const call of calls) {
      fetchMock.mockClear()
      await call()
      expect(calledInit().credentials).toBe('same-origin')
    }
    expect(calls).toHaveLength(10)
  })
})

describe('crew calls surface the server error', () => {
  it('rejects with the server message on a non-OK response', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, {
      error: "crew name 'Leo' is already taken in this repo",
    }))
    await expect(issueRadarApi.createCrew(REF, { name: 'Leo' }))
      .rejects.toThrow("crew name 'Leo' is already taken in this repo")
  })

  it('rejects rather than resolving an empty record on a failed read', async () => {
    // Resolving `{}` here is the dangerous failure: the crew list renders as
    // "no crews yet" and the user is invited to create duplicates.
    fetchMock.mockResolvedValue(jsonResponse(500, { error: 'boom' }))
    await expect(issueRadarApi.crews(REF)).rejects.toThrow('boom')
  })

  it('falls back to the status code when the error body is not json', async () => {
    fetchMock.mockResolvedValue(brokenBody(502))
    await expect(issueRadarApi.crew(REF, 'c_1')).rejects.toThrow('HTTP 502')
  })

  it('rejects a failed work write instead of reporting recorded progress', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, {
      error: 'crew c_1 is already editing #7',
    }))
    await expect(issueRadarApi.recordCrewWork(REF, 'c_1', 9, { phase: 'implementing' }))
      .rejects.toThrow('already editing #7')
  })

  it('returns the parsed body on success', async () => {
    const body = { crew: { id: 'c_beef', name: 'Andromeda' } }
    fetchMock.mockResolvedValue(jsonResponse(200, body))
    await expect(issueRadarApi.updateCrew(REF, 'c_beef', { max_open: 1 })).resolves.toEqual(body)
  })
})

describe('phase classification mirrors crew_store.py', () => {
  it('classifies the terminal phases', async () => {
    expect([...TERMINAL_PHASES].sort()).toEqual(
      ['handed-back', 'preempted', 'resolved', 'skipped', 'yielded'],
    )
  })

  it('ages only pre-PR phases toward the claim ttl', async () => {
    // A parked PR is stronger evidence of a live claim than any heartbeat, so
    // those phases must NOT age toward the TTL.
    expect([...TTL_ACTIVE_PHASES].sort()).toEqual(['claimed', 'implementing', 'investigating'])
    expect(TTL_ACTIVE_PHASES.has('awaiting-ci')).toBe(false)
    expect(TTL_ACTIVE_PHASES.has('awaiting-merge')).toBe(false)
  })

  it('treats exactly the two uncommitted-worktree phases as editing', async () => {
    expect([...EDITING_PHASES].sort()).toEqual(['addressing-review', 'implementing'])
  })

  it('draws every classified phase from the phase list', async () => {
    const known = new Set<string>(CREW_PHASES)
    for (const set of [TERMINAL_PHASES, TTL_ACTIVE_PHASES, EDITING_PHASES]) {
      for (const phase of set) expect(known.has(phase)).toBe(true)
    }
    expect(CREW_PHASES).toHaveLength(13)
  })

  it('never marks a terminal phase as ttl-active or editing', async () => {
    for (const phase of TERMINAL_PHASES) {
      expect(TTL_ACTIVE_PHASES.has(phase)).toBe(false)
      expect(EDITING_PHASES.has(phase)).toBe(false)
    }
  })

  it('countsTowardOpen counts every unfinished phase', async () => {
    // No carve-out: a crew never parks a slot on a human. An issue whose next step
    // belongs to a person is labelled, recorded as a pass and released, so every
    // phase a crew still holds is charged to `max_open`.
    const counted = CREW_PHASES.filter((p) => !TERMINAL_PHASES.has(p))
    expect(counted).toEqual([
      'selected', 'claimed', 'investigating', 'implementing',
      'awaiting-ci', 'addressing-review', 'awaiting-merge', 'awaiting-reply',
    ])
    for (const phase of counted) expect(countsTowardOpen(phase)).toBe(true)
  })

  it('countsTowardOpen rejects every terminal phase', async () => {
    for (const phase of TERMINAL_PHASES) expect(countsTowardOpen(phase)).toBe(false)
  })

  it('classifies every phase in the list', async () => {
    // No phase may be unreachable by the predicate — a new phase added to the
    // union without a decision here would silently count toward a slot.
    for (const phase of CREW_PHASES as readonly CrewPhase[]) {
      expect(typeof countsTowardOpen(phase)).toBe('boolean')
    }
  })
})
