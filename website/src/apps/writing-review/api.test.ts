/**
 * Contract tests for the HTTP client — every route + both error paths.
 *
 * These pin two things:
 *
 * 1. Each ``writingReviewApi.*`` method sends a fetch to the correct
 *    ``/api/apps/writing-review/...`` path with the right method and
 *    body shape, so the wire contract is verified in one place rather
 *    than smuggled through consumer tests.
 * 2. Error responses are surfaced as ``WritingReviewApiError`` with the
 *    machine-readable ``code`` field intact — both the JSON-body and the
 *    non-JSON fallback paths.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { writingReviewApi, WritingReviewApiError } from './api'

type FetchMock = ReturnType<typeof vi.fn>

function stubFetchOnceForSuccess(responseBody: unknown, statusCode = 200): FetchMock {
  const fetchMock = vi.fn().mockResolvedValueOnce({
    ok: statusCode >= 200 && statusCode < 300,
    status: statusCode,
    json: () => Promise.resolve(responseBody),
  } as unknown as Response)
  globalThis.fetch = fetchMock as unknown as typeof fetch
  return fetchMock
}

function stubFetchOnceForError(
  statusCode: number,
  errorBody: unknown | 'invalid-json',
): FetchMock {
  const fetchMock = vi.fn().mockResolvedValueOnce({
    ok: false,
    status: statusCode,
    json: () =>
      errorBody === 'invalid-json'
        ? Promise.reject(new SyntaxError('unexpected token'))
        : Promise.resolve(errorBody),
  } as unknown as Response)
  globalThis.fetch = fetchMock as unknown as typeof fetch
  return fetchMock
}

describe('writingReviewApi', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => {
    // Vitest resets ``globalThis.fetch`` per file; explicit reset here
    // for clarity in case a stub bleeds across tests.
    vi.restoreAllMocks()
  })

  it('listReviews GETs /api/apps/writing-review/reviews', async () => {
    const fetchMock = stubFetchOnceForSuccess({ reviews: [] })
    const responseBody = await writingReviewApi.listReviews()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/apps/writing-review/reviews',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
    expect(responseBody).toEqual({ reviews: [] })
  })

  it('startScan POSTs the ScanRequest payload as JSON to /scan', async () => {
    const fetchMock = stubFetchOnceForSuccess({ job_id: 'j1', status: 'running' })
    const payload = {
      doc_path: '/tmp/foo.md',
      context: { audience: 'team', doc_type: 'update', tone: 'neutral' },
    }
    const responseBody = await writingReviewApi.startScan(payload as never)
    const [urlArg, initArg] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(urlArg).toBe('/api/apps/writing-review/scan')
    expect(initArg.method).toBe('POST')
    expect((initArg.headers as Record<string, string>)['content-type']).toBe('application/json')
    expect(JSON.parse(initArg.body as string)).toEqual(payload)
    expect(responseBody.job_id).toBe('j1')
  })

  it('uploadDocumentFile POSTs multipart/form-data to /uploads with the file part', async () => {
    const fetchMock = stubFetchOnceForSuccess({
      doc_path: '/tmp/uploads/xyz_hero.docx',
      doc_name: 'hero.docx',
    })
    const browsedFile = new File(['fake docx bytes'], 'hero.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    })
    const responseBody = await writingReviewApi.uploadDocumentFile(browsedFile)
    const [urlArg, initArg] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(urlArg).toBe('/api/apps/writing-review/uploads')
    expect(initArg.method).toBe('POST')
    // FormData is opaque to JSON serialisation — assert on presence
    // of the ``file`` field via the FormData API.
    const bodyFormData = initArg.body as FormData
    expect(bodyFormData.get('file')).toBeInstanceOf(File)
    expect((bodyFormData.get('file') as File).name).toBe('hero.docx')
    expect(responseBody.doc_path).toBe('/tmp/uploads/xyz_hero.docx')
  })

  it('getJob URL-encodes the job id path segment', async () => {
    const fetchMock = stubFetchOnceForSuccess({ id: 'a b/c', status: 'running' })
    await writingReviewApi.getJob('a b/c')
    const [urlArg] = fetchMock.mock.calls[0] as [string]
    expect(urlArg).toBe('/api/apps/writing-review/jobs/a%20b%2Fc')
  })

  it('listJobs appends ?status=<status> when a status filter is passed', async () => {
    const fetchMock = stubFetchOnceForSuccess({ jobs: [] })
    await writingReviewApi.listJobs('running')
    const [urlArg] = fetchMock.mock.calls[0] as [string]
    expect(urlArg).toBe('/api/apps/writing-review/jobs?status=running')
  })

  it('listJobs omits the query string when no status is passed', async () => {
    const fetchMock = stubFetchOnceForSuccess({ jobs: [] })
    await writingReviewApi.listJobs()
    const [urlArg] = fetchMock.mock.calls[0] as [string]
    expect(urlArg).toBe('/api/apps/writing-review/jobs')
  })

  it('getReview URL-encodes the review id path segment', async () => {
    const fetchMock = stubFetchOnceForSuccess({ id: 'r1' })
    await writingReviewApi.getReview('r 1')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/writing-review/reviews/r%201')
  })

  it('getReviewContext GETs the /reviews/<id>/context sub-route', async () => {
    const fetchMock = stubFetchOnceForSuccess({ review: {}, document_content: '' })
    await writingReviewApi.getReviewContext('r1')
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/apps/writing-review/reviews/r1/context',
    )
  })

  it('deleteReview issues a DELETE against /reviews/<id>', async () => {
    const fetchMock = stubFetchOnceForSuccess({ deleted: true })
    await writingReviewApi.deleteReview('r1')
    const [, initArg] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(initArg.method).toBe('DELETE')
    // A DELETE with no body MUST NOT set the content-type header so it
    // matches the backend's expectation of an empty request body.
    expect(initArg.headers).toBeUndefined()
  })

  it('getSettings GETs /settings', async () => {
    const fetchMock = stubFetchOnceForSuccess({ max_concurrent: 9 })
    await writingReviewApi.getSettings()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/writing-review/settings')
  })

  it('updateSettings PATCHes /settings with the JSON body', async () => {
    const fetchMock = stubFetchOnceForSuccess({ max_concurrent: 5 })
    const responseBody = await writingReviewApi.updateSettings({ max_concurrent: 5 })
    const [urlArg, initArg] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(urlArg).toBe('/api/apps/writing-review/settings')
    expect(initArg.method).toBe('PATCH')
    expect(JSON.parse(initArg.body as string)).toEqual({ max_concurrent: 5 })
    expect(responseBody).toEqual({ max_concurrent: 5 })
  })

  it('throws WritingReviewApiError with the error+code fields from a JSON error body', async () => {
    stubFetchOnceForError(413, { error: 'upload too big', code: 'upload_too_large' })
    let thrownError: unknown
    try {
      await writingReviewApi.startScan({ doc_text: 'x' } as never)
    } catch (error) {
      thrownError = error
    }
    expect(thrownError).toBeInstanceOf(WritingReviewApiError)
    expect((thrownError as WritingReviewApiError).message).toBe('upload too big')
    expect((thrownError as WritingReviewApiError).code).toBe('upload_too_large')
  })

  it('falls back to "HTTP <status>" when the error body cannot be parsed as JSON', async () => {
    stubFetchOnceForError(500, 'invalid-json')
    let thrownError: unknown
    try {
      await writingReviewApi.getSettings()
    } catch (error) {
      thrownError = error
    }
    expect(thrownError).toBeInstanceOf(WritingReviewApiError)
    expect((thrownError as WritingReviewApiError).message).toBe('HTTP 500')
    // Code defaults to empty string when the server didn't provide one.
    expect((thrownError as WritingReviewApiError).code).toBe('')
  })

  it('propagates WritingReviewApiError from the uploadBinary path', async () => {
    stubFetchOnceForError(400, { error: 'missing filename', code: 'missing_filename' })
    let thrownError: unknown
    try {
      await writingReviewApi.uploadDocumentFile(
        new File(['x'], 'x.docx', { type: 'application/octet-stream' }),
      )
    } catch (error) {
      thrownError = error
    }
    expect(thrownError).toBeInstanceOf(WritingReviewApiError)
    expect((thrownError as WritingReviewApiError).code).toBe('missing_filename')
  })

  it('WritingReviewApiError defaults its code to the empty string when none is passed', () => {
    // The two-arg constructor MUST default ``code`` to '' so a bare
    // error thrown by tests or fallback paths carries a machine-safe
    // sentinel rather than ``undefined``.
    const errorInstance = new WritingReviewApiError('boom')
    expect(errorInstance.code).toBe('')
    expect(errorInstance.name).toBe('WritingReviewApiError')
    expect(errorInstance.message).toBe('boom')
  })
})
