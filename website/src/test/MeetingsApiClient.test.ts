// Meetings API client, tested at the FETCH boundary — deliberately without
// mocking `meetingsApi`.
//
// Two translations happen here and both are silent when they break:
//   • a backend `{"error": …}` body must become the thrown message, or every
//     failure toast in the app degrades to a bare HTTP status text;
//   • the STATUS must survive on the error, because the session hook branches on
//     409 ("another meeting is running") to show a specific message.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const { meetingsApi, MeetingsApiError, safeMeetingId } = await import('../apps/meetings/api')

function response(status: number, body: unknown, { json = true } = {}): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => {
      if (!json) throw new SyntaxError('not json')
      return body
    },
    text: async () => text,
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

describe('meetingsApi transport', () => {
  it('targets the in-gateway /api/apps/meetings base path', async () => {
    fetchMock.mockResolvedValue(response(200, { meetings: [] }))
    await meetingsApi.meetings()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/meetings/meetings')
  })

  it('surfaces the backend error message, not the status text', async () => {
    fetchMock.mockResolvedValue(response(409, { error: 'another meeting is already active' }))
    await expect(meetingsApi.start('m', {})).rejects.toThrow('another meeting is already active')
  })

  it('carries the status so callers can branch on it', async () => {
    fetchMock.mockResolvedValue(response(409, { error: 'busy' }))
    // The session hook shows a DIFFERENT message for 409 than for any other
    // failure, so a stripped status silently regresses that branch.
    await expect(meetingsApi.start('m', {})).rejects.toMatchObject({
      status: 409,
      name: 'MeetingsApiError',
    })
  })

  it('falls back to the status text when the body is not JSON', async () => {
    fetchMock.mockResolvedValue(response(502, '<html>proxy error</html>', { json: false }))
    await expect(meetingsApi.meetings()).rejects.toBeInstanceOf(MeetingsApiError)
    await expect(meetingsApi.meetings()).rejects.toThrow('HTTP 502')
  })

  it('tolerates an empty 204 body', async () => {
    fetchMock.mockResolvedValue(response(204, ''))
    await expect(meetingsApi.resetAgents('m')).resolves.toBeUndefined()
  })

  it('url-encodes a meeting id into the path', async () => {
    fetchMock.mockResolvedValue(response(200, {}))
    await meetingsApi.meeting('evt with space')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/meetings/meetings/evt%20with%20space')
  })

  it('sends JSON with the right verb for each mutation shape', async () => {
    fetchMock.mockResolvedValue(response(200, {}))
    await meetingsApi.dispatch('m', 'hello', true)
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ text: 'hello', chat: true })
    expect(init.headers['Content-Type']).toBe('application/json')

    fetchMock.mockClear()
    await meetingsApi.updateTask('m', 't1', { assignee: 'Alice' })
    expect(fetchMock.mock.calls[0][1].method).toBe('PATCH')

    fetchMock.mockClear()
    await meetingsApi.deleteMeeting('m')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/meetings/meetings/m')
    expect(fetchMock.mock.calls[0][1].method).toBe('DELETE')

    fetchMock.mockClear()
    await meetingsApi.deleteTask('m', 't1')
    expect(fetchMock.mock.calls[0][1].method).toBe('DELETE')
    // A DELETE with a body is unusual enough to be worth pinning: the backend
    // reads the task id from it.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ id: 't1' })

    fetchMock.mockClear()
    await meetingsApi.saveConfig({ task_provider: 'local' } as never)
    expect(fetchMock.mock.calls[0][1].method).toBe('PUT')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      config: { task_provider: 'local' },
    })
  })
})

describe('safeMeetingId', () => {
  it('matches the backend rule for calendar ids', () => {
    // The server's `safe_meeting_id` does exactly this substitution, and the
    // client must agree or every request for a colon-bearing event 404s.
    expect(safeMeetingId('i_AAMk:OG:abc')).toBe('i_AAMk_OG_abc')
  })

  it('leaves a clean id alone', () => {
    expect(safeMeetingId('sprint-standup')).toBe('sprint-standup')
  })
})
