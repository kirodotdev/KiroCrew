/**
 * Code Review Sage HTTP client.
 *
 * fetch is the seam. Each client method must send the right verb, path, query and JSON
 * body, and every failure must surface as a SageApiError carrying the backend's code,
 * the refused character and the message.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SageApiError, sageApi } from './api'

const API = '/api/apps/code-review-sage'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
  )
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const GET = { credentials: 'same-origin' }
const send = (method: string, body?: unknown) => ({
  method,
  credentials: 'same-origin',
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
})

describe('sageApi request shapes', () => {
  it.each([
    ['chatState', () => sageApi.chatState('r1', 'c1'), `${API}/chat?run_id=r1&change_id=c1`, GET],
    ['followupStart', () => sageApi.followupStart('r1', 'c1'), `${API}/followup`, send('POST', { run_id: 'r1', change_id: 'c1' })],
    ['runs', () => sageApi.runs(), `${API}/runs`, GET],
    ['run escapes the id', () => sageApi.run('a/b'), `${API}/runs/a%2Fb`, GET],
    ['runReport', () => sageApi.runReport('r1'), `${API}/runs/r1/report`, GET],
    ['cancelRun', () => sageApi.cancelRun('r1'), `${API}/runs/r1/cancel`, send('POST')],
    ['deleteRun', () => sageApi.deleteRun('r1'), `${API}/runs/r1`, send('DELETE')],
    ['postComments without a selection', () => sageApi.postComments('r1'), `${API}/runs/r1/post`, send('POST', {})],
    ['postComments with keys', () => sageApi.postComments('r1', { changeId: 'c1', keys: ['k'] }), `${API}/runs/r1/post`, send('POST', { change_id: 'c1', keys: ['k'] })],
    ['postComments without keys', () => sageApi.postComments('r1', { changeId: 'c1' }), `${API}/runs/r1/post`, send('POST', { change_id: 'c1' })],
    ['postCommentGroups', () => sageApi.postCommentGroups('r1', [{ changeId: 'c1', keys: ['k'] }, { changeId: 'c2' }]), `${API}/runs/r1/post`, send('POST', { groups: [{ change_id: 'c1', keys: ['k'] }, { change_id: 'c2' }] })],
    ['dismissFinding with a reason', () => sageApi.dismissFinding('r1', 'c1', 'k', 'noise'), `${API}/runs/r1/dismiss`, send('POST', { change_id: 'c1', key: 'k', reason: 'noise' })],
    ['dismissFinding undone', () => sageApi.dismissFinding('r1', 'c1', 'k', null), `${API}/runs/r1/dismiss`, send('POST', { change_id: 'c1', key: 'k', dismissed: false })],
    ['archiveRun', () => sageApi.archiveRun('r1'), `${API}/runs/r1/archive`, send('POST')],
    ['review', () => sageApi.review(['o/r#1']), `${API}/review`, send('POST', { changes: ['o/r#1'] })],
    ['reviewLinks', () => sageApi.reviewLinks('https://x'), `${API}/review`, send('POST', { links: 'https://x' })],
    ['reviewRepo defaults force to false', () => sageApi.reviewRepo('o/r'), `${API}/review-repo`, send('POST', { repo: 'o/r', force: false })],
    ['reviewRepo forced', () => sageApi.reviewRepo('o/r', true), `${API}/review-repo`, send('POST', { repo: 'o/r', force: true })],
    ['recentRepos', () => sageApi.recentRepos(), `${API}/recent-repos`, GET],
    ['recentRepos with a window', () => sageApi.recentRepos(7), `${API}/recent-repos?days=7`, GET],
    ['myRepos', () => sageApi.myRepos(), `${API}/my-repos`, GET],
    ['pinnedRepos', () => sageApi.pinnedRepos(), `${API}/repos`, GET],
    ['pinRepo', () => sageApi.pinRepo('o', 'r'), `${API}/repos`, send('POST', { owner: 'o', repo: 'r' })],
    ['pinRepoUrl', () => sageApi.pinRepoUrl('https://github.com/o/r'), `${API}/repos`, send('POST', { repo: 'https://github.com/o/r' })],
    ['unpinRepo', () => sageApi.unpinRepo('o', 'r'), `${API}/repos`, send('DELETE', { owner: 'o', repo: 'r' })],
    ['repoPrs', () => sageApi.repoPrs('o/r'), `${API}/repo-prs?repo=o%2Fr`, GET],
    ['reviewQueue', () => sageApi.reviewQueue(), `${API}/review-queue`, GET],
    ['settings', () => sageApi.settings(), `${API}/settings`, GET],
    ['putSettings', () => sageApi.putSettings({ autoPost: false }), `${API}/settings`, send('PUT', { autoPost: false })],
    ['namespaces', () => sageApi.namespaces(), `${API}/namespaces`, GET],
    ['createNamespace', () => sageApi.createNamespace('team'), `${API}/namespaces`, send('POST', { name: 'team' })],
    ['deleteNamespace', () => sageApi.deleteNamespace('team'), `${API}/namespaces`, send('DELETE', { name: 'team' })],
    ['learnings escapes the namespace', () => sageApi.learnings('my team'), `${API}/learnings?namespace=my%20team`, GET],
    ['consolidateLearnings', () => sageApi.consolidateLearnings('team'), `${API}/learnings/consolidate`, send('POST', { namespace: 'team' })],
  ])('%s sends the expected verb, path and body', async (_name, call, url, init) => {
    await call()

    expect(fetchMock).toHaveBeenCalledWith(url, init)
  })

  it('returns the parsed JSON body of a successful call', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ runs: [] }), { status: 200 }))

    await expect(sageApi.runs()).resolves.toEqual({ runs: [] })
  })
})

describe('error mapping', () => {
  it('carries the backend message, code and refused character', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'bad name', code: 'invalid_namespace_name', char: '/' }), { status: 422 }),
    )

    const failure = await sageApi.createNamespace('a/b').catch((err: unknown) => err)

    expect(failure).toBeInstanceOf(SageApiError)
    expect(failure).toMatchObject({ message: 'bad name', code: 'invalid_namespace_name', char: '/' })
  })

  it('uses the HTTP status as the message and an empty code when the body names neither', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ char: 7 }), { status: 500 }))

    const failure = (await sageApi.runs().catch((err: unknown) => err)) as SageApiError

    expect(failure.message).toBe('HTTP 500')
    expect(failure.code).toBe('')
    expect(failure.char).toBe('')
  })

  it('falls back to the HTTP status when the error body is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<gateway>', { status: 502 }))

    await expect(sageApi.runs()).rejects.toMatchObject({ message: 'HTTP 502', code: '' })
  })

  it('defaults the refused character to an empty string on the error class', () => {
    expect(new SageApiError('nope', 'code_x').char).toBe('')
  })
})
