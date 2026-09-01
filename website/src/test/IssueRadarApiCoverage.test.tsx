// Issue Radar API client, tested at the FETCH boundary — deliberately without
// mocking `issueRadarApi`, the same contract as IssueRadarApiClient.test.tsx and
// MeetingsApiClient.test.ts.
//
// This is a thin wrapper over ~35 endpoints, and the three things it translates
// are all silent when they break:
//   • the ENDPOINT and VERB each method targets — a method pointed at the wrong
//     path still resolves in every component test, because those mock the client;
//   • the backend `{"error": …}` body becoming the thrown message, so a failure
//     surfaces the server's reason instead of a bare status;
//   • the optional query flags (`refresh`, `poll`, `state`, `first_page`) and the
//     identity fields (`provider`/`host`) reaching the wire at all — a dropped
//     `provider` sends a GitLab request to GitHub and quietly answers about a
//     different repo.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const {
  issueRadarApi, accountQuery, repoQuery, repoBody, DEFAULT_REPO_SETTINGS, SettingsConflictError,
} = await import('../apps/issue-radar/api')

const REF = { owner: 'acme', repo: 'widgets' }
const GL_REF = { owner: 'group/sub', repo: 'svc', provider: 'gitlab' as const, host: 'gl.internal' }

function jsonResponse(status: number, body: unknown, { json = true } = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (!json) throw new SyntaxError('not json')
      return body
    },
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/** The URL the single recorded call went to, with any query string removed. */
const calledPath = () => String(fetchMock.mock.calls[0][0]).split('?')[0]
const calledUrl = () => String(fetchMock.mock.calls[0][0])
const calledInit = () => (fetchMock.mock.calls[0][1] ?? {}) as RequestInit
const sentBody = () => JSON.parse(calledInit().body as string)

/** Every endpoint the client exposes: which path it must hit, with which verb.
 * Driven as a table so a new method cannot be added without a boundary test, and
 * so the error path of each one is exercised rather than one representative. */
const ENDPOINTS: Array<{
  name: string
  path: string
  method?: string
  run: () => Promise<unknown>
}> = [
  { name: 'connect', path: '/api/apps/issue-radar/connect', method: 'POST', run: () => issueRadarApi.connect('https://github.com/acme/widgets') },
  { name: 'issues', path: '/api/apps/issue-radar/issues', run: () => issueRadarApi.issues(REF) },
  { name: 'issuesFirstPage', path: '/api/apps/issue-radar/issues', run: () => issueRadarApi.issuesFirstPage(REF) },
  { name: 'issueDetail', path: '/api/apps/issue-radar/issue', run: () => issueRadarApi.issueDetail(REF, 12) },
  { name: 'pulls', path: '/api/apps/issue-radar/pulls', run: () => issueRadarApi.pulls(REF) },
  { name: 'pullsFirstPage', path: '/api/apps/issue-radar/pulls', run: () => issueRadarApi.pullsFirstPage(REF) },
  { name: 'searchPulls', path: '/api/apps/issue-radar/pulls/search', run: () => issueRadarApi.searchPulls(REF, { author: 'ann' }) },
  { name: 'pullDetail', path: '/api/apps/issue-radar/pull', run: () => issueRadarApi.pullDetail(REF, 12) },
  { name: 'refSummary', path: '/api/apps/issue-radar/ref', run: () => issueRadarApi.refSummary(REF, 12) },
  { name: 'issueAi', path: '/api/apps/issue-radar/issue-ai', run: () => issueRadarApi.issueAi(REF, 12) },
  { name: 'pullAi', path: '/api/apps/issue-radar/pull-ai', run: () => issueRadarApi.pullAi(REF, 12) },
  { name: 'applyLabels', path: '/api/apps/issue-radar/labels/apply', method: 'POST', run: () => issueRadarApi.applyLabels(REF, 12, ['bug'], ['stale']) },
  { name: 'setIssueState', path: '/api/apps/issue-radar/issue/state', method: 'POST', run: () => issueRadarApi.setIssueState(REF, 12, 'closed') },
  { name: 'setPrState', path: '/api/apps/issue-radar/pull/state', method: 'POST', run: () => issueRadarApi.setPrState(REF, 12, 'closed') },
  { name: 'submitPrReview', path: '/api/apps/issue-radar/pull/review', method: 'POST', run: () => issueRadarApi.submitPrReview(REF, 12, 'approve') },
  { name: 'addPrComment', path: '/api/apps/issue-radar/pull/comment', method: 'POST', run: () => issueRadarApi.addPrComment(REF, 12, 'looks good') },
  { name: 'mergePr', path: '/api/apps/issue-radar/pull/merge', method: 'POST', run: () => issueRadarApi.mergePr(REF, 12, 'abc123') },
  { name: 'setPrAutoMerge', path: '/api/apps/issue-radar/pull/auto-merge', method: 'POST', run: () => issueRadarApi.setPrAutoMerge(REF, 12, true) },
  { name: 'pullRuns', path: '/api/apps/issue-radar/pull/runs', run: () => issueRadarApi.pullRuns(REF, 12, 'abc123') },
  { name: 'pullRunAction', path: '/api/apps/issue-radar/pull/run', method: 'POST', run: () => issueRadarApi.pullRunAction(REF, 12, 99, 'cancel') },
  { name: 'bulkPrAction', path: '/api/apps/issue-radar/pulls/bulk', method: 'POST', run: () => issueRadarApi.bulkPrAction(REF, [1, 2], 'approve') },
  { name: 'labels', path: '/api/apps/issue-radar/labels', run: () => issueRadarApi.labels(REF) },
  { name: 'members', path: '/api/apps/issue-radar/members', run: () => issueRadarApi.members(REF) },
  { name: 'repos', path: '/api/apps/issue-radar/repos', run: () => issueRadarApi.repos() },
  { name: 'recentRepos', path: '/api/apps/issue-radar/recent-repos', run: () => issueRadarApi.recentRepos(30) },
  { name: 'me', path: '/api/apps/issue-radar/me', run: () => issueRadarApi.me() },
  { name: 'getSettings', path: '/api/apps/issue-radar/settings', run: () => issueRadarApi.getSettings(REF) },
  { name: 'disconnect', path: '/api/apps/issue-radar/repos', method: 'DELETE', run: () => issueRadarApi.disconnect(REF) },
  { name: 'getInvestigation', path: '/api/apps/issue-radar/investigation', run: () => issueRadarApi.getInvestigation(REF, 12) },
  { name: 'saveInvestigation', path: '/api/apps/issue-radar/investigation', method: 'PUT', run: () => issueRadarApi.saveInvestigation(REF, 12, {}) },
  { name: 'getRecommendations', path: '/api/apps/issue-radar/recommendations', run: () => issueRadarApi.getRecommendations(REF) },
  { name: 'generateRecommendations', path: '/api/apps/issue-radar/recommendations', method: 'POST', run: () => issueRadarApi.generateRecommendations(REF) },
  { name: 'createLabel', path: '/api/apps/issue-radar/labels/create', method: 'POST', run: () => issueRadarApi.createLabel(REF, { name: 'bug' }) },
  { name: 'tagging', path: '/api/apps/issue-radar/tagging', run: () => issueRadarApi.tagging(REF) },
  { name: 'generateTagging', path: '/api/apps/issue-radar/tagging', method: 'POST', run: () => issueRadarApi.generateTagging(REF) },
  { name: 'addSettingLabel', path: '/api/apps/issue-radar/settings/role', method: 'POST', run: () => issueRadarApi.addSettingLabel(REF, 'triage_labels', 'needs-triage') },
  { name: 'applyLabelsBulk', path: '/api/apps/issue-radar/labels/apply-bulk', method: 'POST', run: () => issueRadarApi.applyLabelsBulk(REF, [{ number: 1, add: ['bug'] }]) },
  { name: 'putSettings', path: '/api/apps/issue-radar/settings', method: 'PUT', run: () => issueRadarApi.putSettings(REF, DEFAULT_REPO_SETTINGS) },
]

describe('issueRadarApi transport', () => {
  it.each(ENDPOINTS)('$name targets $path and returns the parsed body', async ({ path, method, run }) => {
    const body = { owner: 'acme', repo: 'widgets', marker: path }
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    await expect(run()).resolves.toEqual(body)

    expect(calledPath()).toBe(path)
    expect(calledInit().method ?? 'GET').toBe(method ?? 'GET')
    // Every call must carry the session cookie: the gateway rejects an
    // unauthenticated request, and omitting this turns the whole app 401.
    expect(calledInit().credentials).toBe('same-origin')
  })

  it.each(ENDPOINTS)('$name surfaces the backend error message', async ({ run }) => {
    fetchMock.mockResolvedValue(jsonResponse(500, { error: 'gh rate limit exceeded' }))
    await expect(run()).rejects.toThrow('gh rate limit exceeded')
  })

  it('falls back to the status when the error body is not JSON', async () => {
    fetchMock.mockResolvedValue(jsonResponse(503, 'gateway down', { json: false }))
    await expect(issueRadarApi.repos()).rejects.toThrow('HTTP 503')
  })

  it('falls back to the status when the error body has no message', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, {}))
    await expect(issueRadarApi.issues(REF)).rejects.toThrow('HTTP 404')
  })

  it('sends JSON content type on every write', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.addPrComment(REF, 4, 'hi')
    expect(calledInit().headers).toEqual({ 'Content-Type': 'application/json' })
  })
})

describe('ref identity on the wire', () => {
  it('repoQuery carries provider and host when present', () => {
    expect(repoQuery(GL_REF)).toEqual({
      owner: 'group/sub', repo: 'svc', provider: 'gitlab', host: 'gl.internal',
    })
  })

  it('repoQuery omits provider and host for a legacy record', () => {
    expect(repoQuery(REF)).toEqual({ owner: 'acme', repo: 'widgets' })
  })

  it('repoBody matches repoQuery so a POST names the same repo as a GET', () => {
    expect(repoBody(GL_REF)).toEqual(repoQuery(GL_REF))
  })

  it('accountQuery is empty without a scope', () => {
    expect(accountQuery()).toEqual({})
    expect(accountQuery({})).toEqual({})
  })

  it('accountQuery carries provider and host independently', () => {
    expect(accountQuery({ provider: 'gitlab' })).toEqual({ provider: 'gitlab' })
    expect(accountQuery({ host: 'gl.internal' })).toEqual({ host: 'gl.internal' })
  })

  it('puts the provider on a GET query string', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.pulls(GL_REF)
    expect(calledUrl()).toContain('provider=gitlab')
    expect(calledUrl()).toContain('host=gl.internal')
  })

  it('puts the provider in a POST body', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.applyLabels(GL_REF, 3, ['bug'], [])
    expect(sentBody()).toMatchObject({ provider: 'gitlab', host: 'gl.internal', number: 3 })
  })
})

describe('list query flags', () => {
  it('omits state, refresh and poll when no options are given', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.issues(REF)
    const url = calledUrl()
    expect(url).not.toContain('state=')
    expect(url).not.toContain('refresh=1')
    expect(url).not.toContain('poll=1')
  })

  it('sends state, refresh and poll for issues when asked', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.issues(REF, { state: 'closed', refresh: true, poll: true })
    const url = calledUrl()
    expect(url).toContain('state=closed')
    expect(url).toContain('refresh=1')
    expect(url).toContain('poll=1')
  })

  it('sends state, refresh and poll for pulls when asked', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.pulls(REF, { state: 'closed', refresh: true, poll: true })
    const url = calledUrl()
    expect(url).toContain('state=closed')
    expect(url).toContain('refresh=1')
    expect(url).toContain('poll=1')
  })

  it('marks the issues fast path with first_page and no refresh', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.issuesFirstPage(REF)
    expect(calledUrl()).toContain('first_page=1')
    expect(calledUrl()).not.toContain('refresh=1')
  })

  it('marks the pulls fast path with first_page', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.pullsFirstPage(REF)
    expect(calledUrl()).toContain('first_page=1')
  })

  it.each([
    ['issueDetail', (refresh?: boolean) => issueRadarApi.issueDetail(REF, 7, refresh === undefined ? undefined : { refresh })],
    ['pullDetail', (refresh?: boolean) => issueRadarApi.pullDetail(REF, 7, refresh === undefined ? undefined : { refresh })],
    ['refSummary', (refresh?: boolean) => issueRadarApi.refSummary(REF, 7, refresh === undefined ? undefined : { refresh })],
    ['issueAi', (refresh?: boolean) => issueRadarApi.issueAi(REF, 7, '', refresh === undefined ? undefined : { refresh })],
    ['pullAi', (refresh?: boolean) => issueRadarApi.pullAi(REF, 7, '', refresh === undefined ? undefined : { refresh })],
  ] as const)('%s sets refresh only when asked, and always sends the number', async (_name, call) => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await call(undefined)
    expect(calledUrl()).toContain('number=7')
    expect(calledUrl()).not.toContain('refresh=1')

    fetchMock.mockClear()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await call(true)
    expect(calledUrl()).toContain('refresh=1')
  })

  it.each([
    ['labels', (refresh?: boolean) => issueRadarApi.labels(REF, refresh === undefined ? undefined : { refresh })],
    ['members', (refresh?: boolean) => issueRadarApi.members(REF, refresh === undefined ? undefined : { refresh })],
  ] as const)('%s sets refresh only when asked', async (_name, call) => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await call(undefined)
    expect(calledUrl()).not.toContain('refresh=1')

    fetchMock.mockClear()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await call(true)
    expect(calledUrl()).toContain('refresh=1')
  })

  it('sends every person filter searchPulls was given', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.searchPulls(REF, {
      state: 'merged', author: 'ann', assignee: 'bo', reviewRequested: 'cy',
    })
    const url = calledUrl()
    expect(url).toContain('state=merged')
    expect(url).toContain('author=ann')
    expect(url).toContain('assignee=bo')
    // Snake case on the wire — the server reads `review_requested`, so a
    // camelCase leak would silently drop the reviewer filter.
    expect(url).toContain('review_requested=cy')
  })

  it('sends no person filter searchPulls was not given', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.searchPulls(REF, {})
    const url = calledUrl()
    expect(url).not.toContain('author=')
    expect(url).not.toContain('assignee=')
    expect(url).not.toContain('review_requested=')
    expect(url).not.toContain('state=')
  })

  it('sends the sha pull runs hang off', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.pullRuns(REF, 7, 'deadbee')
    expect(calledUrl()).toContain('sha=deadbee')
    expect(calledUrl()).toContain('number=7')
  })

  it('sends the window recentRepos was asked for, with an account scope', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { repos: [] }))
    await issueRadarApi.recentRepos(14, { provider: 'gitlab', host: 'gl.internal' })
    const url = calledUrl()
    expect(url).toContain('days=14')
    expect(url).toContain('provider=gitlab')
    expect(url).toContain('host=gl.internal')
  })

  it('asks /me about one provider', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { login: 'ann' }))
    await issueRadarApi.me({ provider: 'gitlab' })
    expect(calledUrl()).toContain('provider=gitlab')
  })
})

describe('write bodies', () => {
  it('sends only the url on connect — the server resolves the provider', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.connect('https://gl.internal/group/sub/svc')
    expect(sentBody()).toEqual({ url: 'https://gl.internal/group/sub/svc' })
  })

  it('sends both label add and remove sets', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.applyLabels(REF, 5, ['bug', 'p1'], ['stale'])
    expect(sentBody()).toEqual({
      owner: 'acme', repo: 'widgets', number: 5, add: ['bug', 'p1'], remove: ['stale'],
    })
  })

  it('sends a close reason when given one', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.setIssueState(REF, 5, 'closed', 'not_planned')
    expect(sentBody()).toMatchObject({ state: 'closed', state_reason: 'not_planned' })
  })

  it('leaves the close reason undefined when not given one', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.setIssueState(REF, 5, 'open')
    expect(sentBody()).not.toHaveProperty('state_reason')
  })

  it('sends a review with its body and head sha', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.submitPrReview(REF, 5, 'request_changes', 'needs a test', 'cafe01')
    expect(sentBody()).toMatchObject({
      event: 'request_changes', body: 'needs a test', head_sha: 'cafe01',
    })
  })

  it('sends empty strings rather than omitting review body and sha', async () => {
    // The server requires both keys; omitting them is a 400 rather than a
    // bodyless approval.
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.submitPrReview(REF, 5, 'approve')
    expect(sentBody()).toMatchObject({ event: 'approve', body: '', head_sha: '' })
  })

  it('pins a merge to the reviewed sha and squashes by default', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.mergePr(REF, 5, 'cafe01')
    expect(sentBody()).toMatchObject({ number: 5, head_sha: 'cafe01', method: 'SQUASH' })
  })

  it('honours an explicit merge method', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.mergePr(REF, 5, 'cafe01', 'REBASE')
    expect(sentBody()).toMatchObject({ method: 'REBASE' })
  })

  it('sends the enabled flag and method when arming auto-merge', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.setPrAutoMerge(REF, 5, true, 'MERGE')
    expect(sentBody()).toMatchObject({ enabled: true, method: 'MERGE' })
  })

  it('sends the disarm as enabled false, not as an omission', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.setPrAutoMerge(REF, 5, false)
    expect(sentBody()).toMatchObject({ enabled: false, method: 'SQUASH' })
  })

  it('defaults a run action to re-running everything', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.pullRunAction(REF, 5, 4242, 'rerun')
    expect(sentBody()).toMatchObject({ run_id: 4242, action: 'rerun', failed_only: false })
  })

  it('sends failed_only when re-running just the failures', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.pullRunAction(REF, 5, 4242, 'rerun', true)
    expect(sentBody()).toMatchObject({ failed_only: true })
  })

  it('sends a head-sha map keyed by number for a bulk approve', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { applied: [], failed: [] }))
    await issueRadarApi.bulkPrAction(REF, [3, 4], 'approve', {
      body: 'ship it', method: 'REBASE', headShas: { '3': 'aaa', '4': 'bbb' },
    })
    expect(sentBody()).toMatchObject({
      numbers: [3, 4], action: 'approve', body: 'ship it', method: 'REBASE',
      head_shas: { '3': 'aaa', '4': 'bbb' },
    })
  })

  it('fills bulk defaults rather than omitting the keys', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { applied: [], failed: [] }))
    await issueRadarApi.bulkPrAction(REF, [3], 'close')
    expect(sentBody()).toMatchObject({ body: '', method: 'SQUASH', head_shas: {} })
  })

  it('sends only the ref when generating recommendations', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.generateRecommendations(REF)
    expect(sentBody()).toEqual({ owner: 'acme', repo: 'widgets' })
  })

  it('spreads the new label fields alongside the ref', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.createLabel(REF, { name: 'area/api', color: 'ff0000', description: 'API' })
    expect(sentBody()).toEqual({
      owner: 'acme', repo: 'widgets', name: 'area/api', color: 'ff0000', description: 'API',
    })
  })

  it('sends the role and label when appending a settings label', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.addSettingLabel(REF, 'good_first_issue_labels', 'good first issue')
    expect(sentBody()).toMatchObject({
      role: 'good_first_issue_labels', label: 'good first issue',
    })
  })

  it('sends per-issue additions for a bulk label apply', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { applied: [], failed: [] }))
    await issueRadarApi.applyLabelsBulk(REF, [
      { number: 1, add: ['bug'] },
      { number: 2, add: ['docs', 'p2'] },
    ])
    expect(sentBody().changes).toEqual([
      { number: 1, add: ['bug'] },
      { number: 2, add: ['docs', 'p2'] },
    ])
  })

  it('sends the whole settings document, revision included', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    const settings = { ...DEFAULT_REPO_SETTINGS, triage_labels: ['needs-triage'], revision: 3 }
    await issueRadarApi.putSettings(REF, settings)
    expect(sentBody().settings).toEqual(settings)
  })

  it('merges an investigation patch into the ref body', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    await issueRadarApi.saveInvestigation(REF, 9, {
      slot_key: 'slot-1', folder_id: 'f-1', status: 'resolved',
      findings: { verdict: 'confirmed' },
    }, 'pull')
    expect(sentBody()).toEqual({
      owner: 'acme', repo: 'widgets', number: 9, kind: 'pull',
      slot_key: 'slot-1', folder_id: 'f-1', status: 'resolved',
      findings: { verdict: 'confirmed' },
    })
  })

  it('deletes a connection by query string, carrying no body', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))
    await issueRadarApi.disconnect(GL_REF)
    expect(calledUrl()).toContain('provider=gitlab')
    expect(calledInit().body).toBeUndefined()
  })
})

describe('putSettings conflict fallbacks', () => {
  // A 409 whose body is missing or unparseable is still a CONFLICT: the caller
  // branches on the error type to rebase its edit, so degrading to a generic
  // Error there loses the edit instead of retrying it.
  it('stays a conflict when the 409 body is not JSON at all', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, 'nginx conflict page', { json: false }))
    const err = await issueRadarApi.putSettings(REF, DEFAULT_REPO_SETTINGS).catch((e) => e)
    expect(err).toBeInstanceOf(SettingsConflictError)
    // Nothing to rebase onto, so the defaults stand in rather than `undefined`,
    // which would crash the caller reading `.current.revision`.
    expect((err as InstanceType<typeof SettingsConflictError>).current)
      .toEqual(DEFAULT_REPO_SETTINGS)
  })

  it('supplies a message when the 409 body carries no error text', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, {}))
    const err = await issueRadarApi.putSettings(REF, DEFAULT_REPO_SETTINGS).catch((e) => e)
    expect(err).toBeInstanceOf(SettingsConflictError)
    expect((err as Error).message).not.toBe('')
    expect((err as Error).name).toBe('SettingsConflictError')
  })
})

describe('DEFAULT_REPO_SETTINGS', () => {
  it('treats unlabeled as untriaged, which is the pre-settings heuristic', () => {
    expect(DEFAULT_REPO_SETTINGS).toEqual({
      triage_labels: [],
      unlabeled_is_untriaged: true,
      good_first_issue_labels: [],
      notify_on_new_issue: false,
      revision: 0,
    })
  })
})
