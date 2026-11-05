/** Meetings API client: verbs, paths, transport parsing, error mapping and the note keepalive cap. fetch is the only stub. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { meetingsApi, MeetingsApiError, NOTE_KEEPALIVE_MAX_BYTES } from './api'

const BASE = '/api/apps/meetings'
const fetchMock = vi.fn()

function reply(status: number, text: string, statusText = '') {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    text: async () => text,
    json: async () => JSON.parse(text),
  }
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockResolvedValue(reply(200, '{}'))
})

afterEach(() => {
  fetchMock.mockReset()
  vi.unstubAllGlobals()
})

describe('transport', () => {
  it('parses a JSON success body and sends a JSON content type with a body', async () => {
    fetchMock.mockResolvedValueOnce(reply(200, '{"config":{"lang":"en"}}'))
    await expect(meetingsApi.saveConfig({ lang: 'en' } as never)).resolves.toEqual({ config: { lang: 'en' } })
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/config`,
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ config: { lang: 'en' } }),
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  it('returns undefined for a 204 and for a blank success body', async () => {
    fetchMock.mockResolvedValueOnce(reply(204, ''))
    await expect(meetingsApi.deleteMeeting('m1')).resolves.toBeUndefined()
    fetchMock.mockResolvedValueOnce(reply(200, '   '))
    await expect(meetingsApi.config()).resolves.toBeUndefined()
  })

  it('throws MeetingsApiError with the server message, status and code', async () => {
    fetchMock.mockResolvedValueOnce(reply(400, '{"error":"bad title","code":"bad_title"}', 'Bad Request'))
    const err = (await meetingsApi.renameMeeting('m1', '').catch((e: unknown) => e)) as MeetingsApiError
    expect(err).toBeInstanceOf(MeetingsApiError)
    expect(err.message).toBe('bad title')
    expect(err.status).toBe(400)
    expect(err.code).toBe('bad_title')
    expect(err.blockingMeetingTitle).toBe('')
  })

  it('carries the blocking meeting from a 409 start refusal', async () => {
    fetchMock.mockResolvedValueOnce(
      reply(
        409,
        JSON.stringify({
          error: 'another meeting is running',
          blocking_meeting_title: 'Standup',
          blocking_meeting_status: 'active',
        }),
      ),
    )
    const err = (await meetingsApi.start('m2', {}).catch((e: unknown) => e)) as MeetingsApiError
    expect(err.status).toBe(409)
    expect(err.blockingMeetingTitle).toBe('Standup')
    expect(err.blockingMeetingStatus).toBe('active')
  })

  it('falls back to the status text when the error body is not JSON or has no error field', async () => {
    fetchMock.mockResolvedValueOnce(reply(502, '<html>gateway</html>', 'Bad Gateway'))
    const html = (await meetingsApi.status().catch((e: unknown) => e)) as MeetingsApiError
    expect(html.message).toBe('Bad Gateway')
    expect(html.code).toBe('')
    fetchMock.mockResolvedValueOnce(reply(500, '{"detail":"x"}', 'Server Error'))
    const noField = (await meetingsApi.status().catch((e: unknown) => e)) as MeetingsApiError
    expect(noField.message).toBe('Server Error')
    expect(noField.code).toBe('')
  })

  it('sends a note image as multipart, leaving the boundary header to the browser', async () => {
    fetchMock.mockResolvedValueOnce(reply(200, '{"markdown":"![a](x)"}'))
    const file = new File(['abc'], 'a.png', { type: 'image/png' })
    await meetingsApi.uploadNoteImage('m1', file)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${BASE}/meetings/m1/note/images`)
    expect(init.body).toBeInstanceOf(FormData)
    expect(init.headers).toBeUndefined()
  })
})

describe('note saves', () => {
  it('keeps a keepalive save only when its body fits the browser cap', async () => {
    await meetingsApi.saveNote('m1', 'short', { keepalive: true })
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'PUT', keepalive: true })

    await meetingsApi.saveNote('m1', 'x'.repeat(NOTE_KEEPALIVE_MAX_BYTES + 1), { keepalive: true })
    expect(fetchMock.mock.calls[1][1].keepalive).toBe(false)

    await meetingsApi.saveNote('m1', 'short')
    expect(fetchMock.mock.calls[2][1].keepalive).toBe(false)
  })

})

describe('endpoints', () => {
  const cases: Array<[string, () => Promise<unknown>, string, string]> = [
    ['dictionary', () => meetingsApi.dictionary(), 'GET', '/dictionary'],
    ['addTerm', () => meetingsApi.addTerm('Kiro', ['kiro']), 'POST', '/dictionary'],
    ['removeTerm', () => meetingsApi.removeTerm('Kiro'), 'POST', '/dictionary/remove'],
    ['calendar', () => meetingsApi.calendar(), 'GET', '/calendar'],
    ['syncCalendar', () => meetingsApi.syncCalendar(), 'POST', '/calendar/sync'],
    ['agents', () => meetingsApi.agents(), 'GET', '/agents'],
    ['status', () => meetingsApi.status(), 'GET', '/status'],
    ['meetings', () => meetingsApi.meetings(), 'GET', '/meetings'],
    ['meeting', () => meetingsApi.meeting('a/b'), 'GET', '/meetings/a%2Fb'],
    ['renameMeeting', () => meetingsApi.renameMeeting('m1', 'T'), 'PATCH', '/meetings/m1'],
    ['deleteMeeting', () => meetingsApi.deleteMeeting('m1'), 'DELETE', '/meetings/m1'],
    ['init', () => meetingsApi.init('m1', 'T'), 'POST', '/meetings/m1/init'],
    ['start', () => meetingsApi.start('m1', {}), 'POST', '/meetings/m1/start'],
    ['setStatus', () => meetingsApi.setStatus('m1', 'active'), 'POST', '/meetings/m1/status'],
    ['stop', () => meetingsApi.stop('m1'), 'POST', '/meetings/m1/stop'],
    ['outputs', () => meetingsApi.outputs('m1'), 'GET', '/meetings/m1/outputs'],
    ['saveOutput', () => meetingsApi.saveOutput('m1', 'a1', 'text'), 'PUT', '/meetings/m1/outputs'],
    ['revertOutput', () => meetingsApi.revertOutput('m1', 'a1'), 'DELETE', '/meetings/m1/outputs'],
    ['transcript', () => meetingsApi.transcript('m1'), 'GET', '/meetings/m1/transcript'],
    ['transcript cursor', () => meetingsApi.transcript('m1', 5), 'GET', '/meetings/m1/transcript?cursor=5'],
    ['note', () => meetingsApi.note('m1'), 'GET', '/meetings/m1/note'],
    ['translations', () => meetingsApi.translations('m1', 3), 'GET', '/meetings/m1/translations?since=3'],
    ['attachments', () => meetingsApi.attachments('m1', { action: 'remove', index: 0 }), 'POST', '/meetings/m1/attachments'],
    ['toggleAgent', () => meetingsApi.toggleAgent('m1', 'a1', true), 'POST', '/meetings/m1/agents'],
    ['mute', () => meetingsApi.mute('m1', 'a1', true), 'POST', '/meetings/m1/mute'],
    ['dispatch', () => meetingsApi.dispatch('m1', 'hi', true), 'POST', '/meetings/m1/dispatch'],
    ['message', () => meetingsApi.message('m1', 'a1', 'hi'), 'POST', '/meetings/m1/message'],
    ['resetAgents', () => meetingsApi.resetAgents('m1'), 'POST', '/meetings/m1/reset'],
    ['tasks', () => meetingsApi.tasks('m1'), 'GET', '/meetings/m1/tasks'],
    ['addTask', () => meetingsApi.addTask('m1', 'Send notes'), 'POST', '/meetings/m1/tasks'],
    ['updateTask', () => meetingsApi.updateTask('m1', 't1', { priority: 'high' }), 'PATCH', '/meetings/m1/tasks'],
    ['deleteTask', () => meetingsApi.deleteTask('m1', 't1'), 'DELETE', '/meetings/m1/tasks'],
    ['fileTask', () => meetingsApi.fileTask('m1', 't1'), 'POST', '/meetings/m1/tasks/file'],
    ['reviewTask', () => meetingsApi.reviewTask('m1', 't1', 'archived'), 'POST', '/meetings/m1/tasks/review'],
    ['taskProviders', () => meetingsApi.taskProviders(), 'GET', '/task-providers'],
  ]

  it.each(cases)('%s reaches its verb and path', async (_name, call, method, path) => {
    await call()
    const [url, init] = fetchMock.mock.lastCall as [string, RequestInit | undefined]
    expect(url).toBe(`${BASE}${path}`)
    expect(init?.method ?? 'GET').toBe(method)
  })

})
