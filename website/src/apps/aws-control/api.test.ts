/**
 * Tests for the AWS Control API client (`./api`).
 *
 * This is a thin same-origin fetch wrapper, so the contract worth pinning is
 * the REQUEST each method builds (path, method, query params, JSON vs raw body,
 * headers, credentials) and how the shared `request` helper maps responses and
 * error statuses — the body's machine-readable `code` over the English prose,
 * with a `http_<status>` fallback when there is no usable code. Asserting only
 * "fetch was called" would let a wrong path or a dropped `confirm:true` slip by.
 *
 * Fetch-mocking idiom copied from the sibling `../issue-radar/pipeline/api.test.ts`:
 * spy on `globalThis.fetch`, resolve a real `Response`, restore after each.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { awsControlApi, AwsControlError, errorReportOf } from './api'
import { __resetErrorJournalForTests, recentErrors } from '../../utils/errorReport'

const BASE = '/api/apps/aws-control'

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

/** The [url, init] fetch was called with on its first (or only) invocation. */
function firstCall(spy: ReturnType<typeof vi.spyOn>): [string, RequestInit] {
  return spy.mock.calls[0] as [string, RequestInit]
}

/** Parse the request URL into pathname + query for order-independent assertions. */
function parseUrl(url: string): URL {
  return new URL(url, 'http://localhost')
}

let fetchSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  fetchSpy = vi.spyOn(globalThis, 'fetch')
  __resetErrorJournalForTests()
})

afterEach(() => {
  fetchSpy.mockRestore()
})

/* ── The shared request/error contract ──────────────────────────────────── */

describe('request error contract (AwsControlError)', () => {
  it('prefers the body\'s machine-readable `code` over the English prose on a non-ok response', async () => {
    // The UI localises off the token, so `code` must win even when `error` is set.
    fetchSpy.mockResolvedValue(jsonResponse({ code: 'app_disabled', error: 'App is disabled' }, 403))

    const err = await awsControlApi.iamPolicy().then(
      () => { throw new Error('expected rejection') },
      (e) => e as unknown,
    )
    expect(err).toBeInstanceOf(AwsControlError)
    expect((err as AwsControlError).message).toBe('app_disabled')
    expect((err as AwsControlError).name).toBe('AwsControlError')
    expect((err as AwsControlError).status).toBe(403)
  })

  it('falls back to `http_<status>` when the error body is JSON without a string `code`', async () => {
    // A body whose `code` is absent or non-string carries no usable token.
    fetchSpy.mockResolvedValue(jsonResponse({ error: 'boom', code: 42 }, 500))
    const err = await awsControlApi.iamPolicy().catch((e) => e as AwsControlError)
    expect(err).toBeInstanceOf(AwsControlError)
    expect(err.message).toBe('http_500')
    expect(err.status).toBe(500)
  })

  it('falls back to `http_<status>` when the error body is not JSON', async () => {
    // The `res.json()` throws and is swallowed, leaving the status-derived token.
    fetchSpy.mockResolvedValue(rawResponse('<html>502 Bad Gateway</html>', 502))
    const err = await awsControlApi.accounts().catch((e) => e as AwsControlError)
    expect(err.message).toBe('http_502')
    expect(err.status).toBe(502)
  })

  it('rejects (does not swallow) a transport-level failure', async () => {
    // fetch itself rejecting must propagate — the client has no offline fallback.
    fetchSpy.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(awsControlApi.accounts()).rejects.toThrow('Failed to fetch')
  })

  it('journals a non-ok response and hands the entry to the error, query string stripped', async () => {
    // The UI renders a LOCALISED sentence, never the backend prose, so the shared
    // notice's message-match lookup can never find this failure. The report has
    // to travel on the error itself — with the endpoint, status, code and raw
    // body the agent needs, and WITHOUT the query string (an object key or a
    // share note does not belong in a prompt).
    fetchSpy.mockResolvedValue(
      jsonResponse({ error: 'AccessDenied on GetObject', code: 'aws_call_failed' }, 502),
    )
    const err = await awsControlApi
      .driveDownload('111122223333', 'drive', 'secret folder/report.pdf')
      .catch((e) => e as AwsControlError)

    expect(err).toBeInstanceOf(AwsControlError)
    expect(err.report).toBeDefined()
    expect(err.report).toMatchObject({
      source: 'api',
      status: 502,
      code: 'aws_call_failed',
      message: 'AccessDenied on GetObject',
      endpoint: `${BASE}/drive/111122223333/download`,
    })
    expect(err.report?.detail).toContain('AccessDenied on GetObject')
    expect(err.report?.endpoint).not.toContain('secret')
    // The same entry is in the journal, and `errorReportOf` returns exactly it.
    expect(recentErrors()[0]).toBe(err.report)
    expect(errorReportOf(err)).toBe(err.report)
  })

  it('journals a non-JSON body under the status code, so a 502 page still reaches the agent', async () => {
    fetchSpy.mockResolvedValue(rawResponse('<html>502 Bad Gateway</html>', 502))
    const err = await awsControlApi.accounts().catch((e) => e as AwsControlError)
    expect(err.report).toMatchObject({ status: 502, code: 'http_502', message: 'http_502' })
    expect(err.report?.detail).toContain('502 Bad Gateway')
  })

  it('journals a transport failure and recovers it by message, without changing the thrown error', async () => {
    fetchSpy.mockRejectedValue(new TypeError('Failed to fetch'))
    const err = await awsControlApi.accounts().catch((e) => e as unknown)
    expect(err).toBeInstanceOf(TypeError)
    const report = errorReportOf(err)
    expect(report).toMatchObject({
      source: 'api',
      message: 'Failed to fetch',
      endpoint: `${BASE}/accounts`,
    })
    expect(report?.status).toBeUndefined()
  })

  it('errorReportOf answers undefined for an error built outside the client', () => {
    // Tests construct `AwsControlError` directly; the notice must degrade to the
    // sentence alone rather than throw on a missing report.
    expect(errorReportOf(new AwsControlError('app_disabled', 403))).toBeUndefined()
    expect(errorReportOf('not an error')).toBeUndefined()
    expect(errorReportOf(undefined)).toBeUndefined()
  })

  it('sends same-origin credentials and no method on a plain GET', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ policy: '{}' }))
    await awsControlApi.iamPolicy()
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/iam-policy`)
    expect(init.credentials).toBe('same-origin')
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('returns the parsed JSON body verbatim on a 200', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ policy: '{"Statement":[]}' }))
    const res = await awsControlApi.iamPolicy()
    expect(res).toEqual({ policy: '{"Statement":[]}' })
  })
})

/* ── Accounts / reconnect / iam-policy (top-level reads) ─────────────────── */

describe('awsControlApi.accounts', () => {
  it('GETs /accounts with no query by default', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ accounts: [], totals: {}, generatedAt: '' }))
    await awsControlApi.accounts()
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/accounts`)
  })

  it('appends ?refresh=1 only when refresh is requested', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ accounts: [], totals: {}, generatedAt: '' }))
    await awsControlApi.accounts(true)
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/accounts?refresh=1`)
  })
})

describe('awsControlApi.reconnectPlan', () => {
  it('percent-encodes the profile name into the path segment', async () => {
    // A profile name with a slash must not open a new path segment on the server.
    fetchSpy.mockResolvedValue(jsonResponse({ method: 'terminal', kind: 'sso', command: 'x' }))
    await awsControlApi.reconnectPlan('team/prod')
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/profiles/team%2Fprod/reconnect-plan`)
  })
})

describe('awsControlApi.iamPolicy', () => {
  it('GETs /iam-policy', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ policy: '{}' }))
    await awsControlApi.iamPolicy()
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/iam-policy`)
  })
})

/* ── Drive: status + bootstrap ───────────────────────────────────────────── */

describe('awsControlApi.drive', () => {
  it('GETs /drive/{account}, encoding the account, without refresh by default', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ exists: false }))
    await awsControlApi.drive('acct 1')
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/drive/acct%201`)
  })

  it('appends ?refresh=1 to bypass the usage cache', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ exists: false }))
    await awsControlApi.drive('a', true)
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/drive/a?refresh=1`)
  })
})

describe('awsControlApi.driveBootstrapPreview / driveBootstrapConfirm', () => {
  it('preview POSTs an EMPTY body (no side effect) with the JSON content type', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ preview: true, account: 'a', region: 'us-west-2', resource: 'x' }))
    await awsControlApi.driveBootstrapPreview('a')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/drive/a/bootstrap`)
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(init.body).toBe('{}')
  })

  it('confirm POSTs {confirm:true} to the SAME path — the flag is what creates the bucket', async () => {
    // Preview and confirm share a URL; only the body distinguishes read from write.
    fetchSpy.mockResolvedValue(jsonResponse({ created: true, bucket: 'b' }))
    await awsControlApi.driveBootstrapConfirm('a')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/drive/a/bootstrap`)
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ confirm: true }))
  })
})

/* ── Drive: list / download / upload / delete / share ────────────────────── */

describe('awsControlApi.driveList', () => {
  it('carries only section when path and token are empty', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ files: [], folders: [] }))
    await awsControlApi.driveList('a', 'drive')
    const parsed = parseUrl(firstCall(fetchSpy)[0])
    expect(parsed.pathname).toBe(`${BASE}/drive/a/list`)
    expect(parsed.searchParams.get('section')).toBe('drive')
    expect(parsed.searchParams.has('path')).toBe(false)
    expect(parsed.searchParams.has('token')).toBe(false)
  })

  it('adds path and token to the query only when they are non-empty', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ files: [], folders: [] }))
    await awsControlApi.driveList('a', 'library', 'sub/folder', 'pg2')
    const parsed = parseUrl(firstCall(fetchSpy)[0])
    expect(parsed.searchParams.get('section')).toBe('library')
    expect(parsed.searchParams.get('path')).toBe('sub/folder')
    expect(parsed.searchParams.get('token')).toBe('pg2')
  })
})

describe('awsControlApi.driveDownload', () => {
  it('GETs /download with section and key in the query', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ url: 'https://x', expiresSecs: 60 }))
    await awsControlApi.driveDownload('a', 'backup', 'path/to/file.txt')
    const parsed = parseUrl(firstCall(fetchSpy)[0])
    expect(parsed.pathname).toBe(`${BASE}/drive/a/download`)
    expect(parsed.searchParams.get('section')).toBe('backup')
    expect(parsed.searchParams.get('key')).toBe('path/to/file.txt')
  })
})

describe('awsControlApi.driveUpload', () => {
  it('POSTs the raw Blob as the body (no JSON content type) with section+key in the query', async () => {
    // Upload sends raw bytes, so the body must be the Blob itself — not JSON-encoded —
    // and no Content-Type header is forced, letting the Blob's own type ride.
    fetchSpy.mockResolvedValue(jsonResponse({ uploaded: true, key: 'k', bytes: 3 }))
    const blob = new Blob(['abc'], { type: 'text/plain' })
    await awsControlApi.driveUpload('a', 'drive', 'k', blob)
    const [url, init] = firstCall(fetchSpy)
    const parsed = parseUrl(url)
    expect(parsed.pathname).toBe(`${BASE}/drive/a/upload`)
    expect(parsed.searchParams.get('section')).toBe('drive')
    expect(parsed.searchParams.get('key')).toBe('k')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(blob)
    expect(init.headers).toBeUndefined()
  })
})

describe('awsControlApi.driveDelete', () => {
  it('POSTs {section,key} as JSON to /delete', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ deleted: true }))
    await awsControlApi.driveDelete('a', 'drive', 'k')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/drive/a/delete`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ section: 'drive', key: 'k' })
  })
})

describe('awsControlApi.driveShare', () => {
  it('POSTs the full share request (section,key,expiresSecs,note) as JSON', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ url: 'https://x', share: {} }))
    await awsControlApi.driveShare('a', 'library', 'k', 3600, 'for review')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/drive/a/share`)
    expect(JSON.parse(init.body as string)).toEqual({
      section: 'library',
      key: 'k',
      expiresSecs: 3600,
      note: 'for review',
    })
  })
})

/* ── Shares ledger ───────────────────────────────────────────────────────── */

describe('awsControlApi.shares / shareForget', () => {
  it('shares GETs /shares with the account as a query param', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ shares: [] }))
    await awsControlApi.shares('acct/with/slash')
    const parsed = parseUrl(firstCall(fetchSpy)[0])
    expect(parsed.pathname).toBe(`${BASE}/shares`)
    expect(parsed.searchParams.get('account')).toBe('acct/with/slash')
  })

  it('shareForget POSTs an empty body to /shares/{id}/forget with the id encoded', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ forgotten: true }))
    await awsControlApi.shareForget('id/1')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/shares/id%2F1/forget`)
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{}')
  })
})

/* ── Costs / library ─────────────────────────────────────────────────────── */

describe('awsControlApi.costs', () => {
  it('GETs /costs/{account}, appending ?refresh=1 only when asked', async () => {
    // A fresh Response per call: a Response body can only be read once, so the
    // second cost fetch needs its own object rather than a shared resolved value.
    const cost = () => jsonResponse({ fresh: true, monthToDate: 0, projected: 0, currency: 'USD', byService: [], fetchedAt: '' })
    fetchSpy.mockImplementation(() => Promise.resolve(cost()))
    await awsControlApi.costs('a')
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/costs/a`)

    fetchSpy.mockClear()
    await awsControlApi.costs('a', true)
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/costs/a?refresh=1`)
  })
})

describe('awsControlApi.library / libraryPush', () => {
  it('library GETs /library/{account}', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ artifacts: [] }))
    await awsControlApi.library('a')
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/library/a`)
  })

  it('libraryPush POSTs {slug} as JSON to /library/{account}/push', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ pushed: true }))
    await awsControlApi.libraryPush('a', 'my-slug')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/library/a/push`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ slug: 'my-slug' })
  })
})

/* ── Backup ──────────────────────────────────────────────────────────────── */

describe('awsControlApi.backup*', () => {
  it('backup GETs /backup/{account}', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ nightly: false, runs: {}, remote: null }))
    await awsControlApi.backup('a')
    expect(firstCall(fetchSpy)[0]).toBe(`${BASE}/backup/a`)
  })

  it('backupRun POSTs {kind} to /backup/{account}/run', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ ran: true, kind: 'snapshot', run: {} }))
    await awsControlApi.backupRun('a', 'snapshot')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/backup/a/run`)
    expect(JSON.parse(init.body as string)).toEqual({ kind: 'snapshot' })
  })

  it('backupNightly POSTs the boolean enabled flag verbatim (including false)', async () => {
    // `false` must be sent, not dropped — the JSON body preserves the toggle state.
    fetchSpy.mockResolvedValue(jsonResponse({ nightly: false }))
    await awsControlApi.backupNightly('a', false)
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/backup/a/nightly`)
    expect(JSON.parse(init.body as string)).toEqual({ enabled: false })
  })

  it('backupRestore POSTs {key} to /backup/{account}/restore', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ downloaded: true, path: '/tmp/x', bytes: 1 }))
    await awsControlApi.backupRestore('a', 'archive/2026.tar')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/backup/a/restore`)
    expect(JSON.parse(init.body as string)).toEqual({ key: 'archive/2026.tar' })
  })
})

/* ── profile discovery + registration ─────────────────────────────────────── */

describe('awsControlApi.driveFolderCreate / driveFolderDelete', () => {
  it('create POSTs the section and path, encoding the account into the segment', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ created: true, path: 'photos/2026' }))
    await awsControlApi.driveFolderCreate('acct/1', 'drive', 'photos/2026')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/drive/acct%2F1/folder`)
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    // The path travels in the BODY, not the URL: a folder path contains slashes,
    // and putting it in the path would make it indistinguishable from further
    // route segments.
    expect(init.body).toBe(JSON.stringify({ section: 'drive', path: 'photos/2026' }))
  })

  it('delete POSTs to its own path, so a create can never be mistaken for a delete', async () => {
    // Distinct URLs rather than one path plus a flag: unlike bootstrap's
    // preview/confirm pair, these are two different operations and a dropped
    // field must not turn one into the other.
    fetchSpy.mockResolvedValue(jsonResponse({ deleted: true, path: 'photos', objects: 12 }))
    await awsControlApi.driveFolderDelete('a', 'drive', 'photos')
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/drive/a/folder/delete`)
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ section: 'drive', path: 'photos' }))
  })

  it('carries the section through, so a folder is created in the section asked for', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ created: true, path: 'q' }))
    await awsControlApi.driveFolderCreate('a', 'backup', 'q')
    const [, init] = firstCall(fetchSpy)
    expect(JSON.parse(String(init.body)).section).toBe('backup')
  })

  it('returns the object count the delete reports, which is what the UI restates', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ deleted: true, path: 'photos', objects: 12 }))
    const res = await awsControlApi.driveFolderDelete('a', 'drive', 'photos')
    expect(res.objects).toBe(12)
  })

  it('surfaces the backend refusal code for a path that escapes its section', async () => {
    // The backend runs the path through the same key validator every object key
    // goes through; the client must not swallow that refusal.
    fetchSpy.mockResolvedValue(jsonResponse({ code: 'invalid_key' }, 400))
    await expect(awsControlApi.driveFolderCreate('a', 'drive', '../etc')).rejects.toBeInstanceOf(AwsControlError)
  })
})

describe('awsControlApi.availableProfiles / registerProfiles', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('reads the discovery listing with a plain GET and no query', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ profiles: [], registeredCount: 0, max: 50, supported: true }),
    )
    await awsControlApi.availableProfiles()
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/profiles/available`)
    expect(init.method ?? 'GET').toBe('GET')
    expect(init.credentials).toBe('same-origin')
  })

  it('returns the supported flag so the caller can explain an empty list', async () => {
    // An empty list on a host that cannot enumerate profiles is NOT "you have
    // none", so the flag has to survive the client untouched.
    fetchSpy.mockResolvedValue(
      jsonResponse({ profiles: [], registeredCount: 4, max: 50, supported: false }),
    )
    const res = await awsControlApi.availableProfiles()
    expect(res.supported).toBe(false)
    expect(res.registeredCount).toBe(4)
  })

  it('posts the selected names as a JSON body', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ added: 2, skipped: 1 }))
    const res = await awsControlApi.registerProfiles(['alpha', 'beta', 'beta'])
    const [url, init] = firstCall(fetchSpy)
    expect(url).toBe(`${BASE}/profiles/register`)
    expect(init.method).toBe('POST')
    expect(String((init.headers as Record<string, string>)['Content-Type'])).toContain(
      'application/json',
    )
    expect(JSON.parse(String(init.body))).toEqual({ names: ['alpha', 'beta', 'beta'] })
    expect(res).toEqual({ added: 2, skipped: 1 })
  })

  it('surfaces the backend refusal code rather than its prose', async () => {
    // The page keys its message off the code, and `AwsControlError` carries it
    // as the Error MESSAGE (see its constructor) -- not as a `code` field, which
    // is the trap when writing this assertion.
    fetchSpy.mockResolvedValue(
      jsonResponse({ error: '1 name(s) are not profiles on this machine', code: 'unknown_profile' }, 400),
    )
    await expect(awsControlApi.registerProfiles(['planted'])).rejects.toMatchObject({
      message: 'unknown_profile',
      status: 400,
    })
    await expect(awsControlApi.registerProfiles(['planted'])).rejects.toBeInstanceOf(
      AwsControlError,
    )
  })
})
