// Meetings API client, tested at the FETCH boundary — deliberately without
// mocking `meetingsApi`.
//
// Two translations happen here and both are silent when they break:
//   • a backend `{"error": …}` body must become the thrown message, or every
//     failure toast in the app degrades to a bare HTTP status text;
//   • status and machine code must survive on the error, because the session hook
//     distinguishes conflicts from permanent transcript-capacity failures.

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

  it('carries the backend code so permanent failures are not retried', async () => {
    fetchMock.mockResolvedValue(response(413, {
      error: 'meeting transcript is too large',
      code: 'transcript_too_large',
    }))

    await expect(meetingsApi.dispatch('m', 'hello')).rejects.toMatchObject({
      status: 413,
      code: 'transcript_too_large',
    })
  })

  it('adds the opaque cursor only to incremental transcript requests', async () => {
    fetchMock.mockResolvedValue(response(200, { segments: [], next_cursor: 42 }))

    await meetingsApi.transcript('m')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/meetings/meetings/m/transcript')

    fetchMock.mockClear()
    await meetingsApi.transcript('m', 42)
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/apps/meetings/meetings/m/transcript?cursor=42',
    )
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

describe('meetingsApi uploadAudio', () => {
  it('POSTs the recording as multipart to the meeting audio endpoint', async () => {
    fetchMock.mockResolvedValue(response(200, { ok: true, bytes: 9 }))
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/webm;codecs=opus' })
    await expect(meetingsApi.uploadAudio('standup', blob)).resolves.toEqual({ ok: true })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/apps/meetings/meetings/standup/audio')
    expect(init.method).toBe('POST')
    // Multipart: a FormData body, NOT a JSON Content-Type (which would corrupt it).
    expect(init.body).toBeInstanceOf(FormData)
    expect(init.headers?.['Content-Type']).toBeUndefined()
    // The 'audio' part carries the recording blob. (jsdom's FormData does not
    // preserve the append() filename, so assert the payload, not the name.)
    const part = (init.body as FormData).get('audio')
    expect(part).toBeInstanceOf(Blob)
    expect((part as Blob).type).toBe('audio/webm;codecs=opus')
  })

  it('encodes the meeting id and accepts each recognised codec', async () => {
    for (const type of ['audio/mp4', 'audio/ogg;codecs=opus', 'audio/webm', '']) {
      fetchMock.mockClear()
      fetchMock.mockResolvedValue(response(200, { ok: true }))
      await expect(
        meetingsApi.uploadAudio('evt space', new Blob(['x'], { type })),
      ).resolves.toEqual({ ok: true })
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/apps/meetings/meetings/evt%20space/audio',
      )
    }
  })

  it('surfaces the backend error message and code on failure', async () => {
    fetchMock.mockResolvedValue(response(413, {
      error: 'recording exceeds the size limit',
      code: 'recording_too_large',
    }))
    await expect(
      meetingsApi.uploadAudio('m', new Blob(['x'], { type: 'audio/webm' })),
    ).rejects.toMatchObject({
      name: 'MeetingsApiError',
      status: 413,
      code: 'recording_too_large',
    })
  })

  it('falls back to the status text when the error body is not JSON', async () => {
    fetchMock.mockResolvedValue(response(500, '<html>err</html>', { json: false }))
    await expect(
      meetingsApi.uploadAudio('m', new Blob(['x'], { type: 'audio/webm' })),
    ).rejects.toThrow('HTTP 500')
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
