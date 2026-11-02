/**
 * The design-critique API client: the slot calls, the reply parsing for empty and
 * JSON bodies, the inline-versus-job discover and render flows, and the job poll
 * (its 1.5 s cadence, the error status and the tolerance for a few lost polls).
 * fetch is stubbed; the poll's timer is faked so the cadence is driven, not waited.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { designCritiqueApi } from './api'

const DC = '/api/apps/design-critique'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

let fetchMock: ReturnType<typeof vi.fn>

/** Advance the fake clock one poll interval at a time until the promise settles. */
async function settle<T>(promise: Promise<T>): Promise<T> {
  let done = false
  promise.then(() => { done = true }, () => { done = true })
  for (let i = 0; i < 20 && !done; i++) await vi.advanceTimersByTimeAsync(1500)
  return promise
}

beforeEach(() => {
  vi.useFakeTimers()
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('designCritiqueApi slot calls', () => {
  it('opens a temporary design-critique slot with a dc- name', async () => {
    fetchMock.mockResolvedValueOnce(json({ key: 'slot-1' }))
    await expect(designCritiqueApi.openSlot()).resolves.toEqual({ key: 'slot-1' })
    const [path, init] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/chat/slots')
    expect(init.method).toBe('POST')
    const body = JSON.parse(init.body)
    expect(body.name).toMatch(/^dc-\d+$/)
    expect(body).toMatchObject({ memory_mode: 'temporary', mode: 'design-critique' })
  })

  it('encodes the slot key in the path when reading a slot', async () => {
    fetchMock.mockResolvedValueOnce(json({ messages: [] }))
    await designCritiqueApi.getSlot('a/b')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/slots/a%2Fb')
  })

  it('reads an empty reply body as undefined', async () => {
    fetchMock.mockResolvedValueOnce(new Response('', { status: 200 }))
    await expect(designCritiqueApi.getSlot('s')).resolves.toBeUndefined()
  })

  it('treats a 204 delete as success and swallows a failed delete', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }))
    await expect(designCritiqueApi.deleteSlot('s')).resolves.toBeUndefined()
    fetchMock.mockResolvedValueOnce(json({ error: 'gone' }, 500))
    await expect(designCritiqueApi.deleteSlot('s')).resolves.toBeUndefined()
  })

  it('throws on a failed read', async () => {
    fetchMock.mockResolvedValueOnce(json({ error: 'nope' }, 404))
    await expect(designCritiqueApi.getSlot('s')).rejects.toThrow()
  })

  it('loads the critique method text', async () => {
    fetchMock.mockResolvedValueOnce(json({ checklist: 'Check contrast' }))
    await expect(designCritiqueApi.method()).resolves.toEqual({ checklist: 'Check contrast' })
    expect(fetchMock.mock.calls[0][0]).toBe(`${DC}/method`)
  })
})

describe('designCritiqueApi discover', () => {
  it('returns an inline payload without starting a job', async () => {
    const onJob = vi.fn()
    fetchMock.mockResolvedValueOnce(json({ screens: [{ id: 'a' }], handle: 'h' }))
    const scope = await designCritiqueApi.discover('url', 'https://x', onJob)
    expect(scope).toEqual({ screens: [{ id: 'a' }], handle: 'h' })
    expect(onJob).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('reports the job id, then polls until the run is done', async () => {
    const onJob = vi.fn()
    fetchMock
      .mockResolvedValueOnce(json({ job: 'j1' }))
      .mockResolvedValueOnce(json({ status: 'running' }))
      .mockResolvedValueOnce(json({ status: 'done', result: { screens: [], handle: 'h2' } }))
    const result = await settle(designCritiqueApi.discover('repo', 'owner/name', onJob))
    expect(onJob).toHaveBeenCalledWith('j1')
    expect(result).toEqual({ screens: [], handle: 'h2' })
    expect(fetchMock.mock.calls[1][0]).toBe(`${DC}/discover?job=j1`)
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('reconnects to a job by id without posting again', async () => {
    fetchMock.mockResolvedValueOnce(json({ status: 'done', result: { screens: [{ id: 'z' }] } }))
    await expect(settle(designCritiqueApi.pollDiscover('j9'))).resolves.toEqual({ screens: [{ id: 'z' }] })
    expect(fetchMock.mock.calls[0][0]).toBe(`${DC}/discover?job=j9`)
    expect(fetchMock.mock.calls[0][1].method).toBeUndefined()
  })

  it('rejects with the job error, or a default message when it gives none', async () => {
    fetchMock
      .mockResolvedValueOnce(json({ job: 'j2' }))
      .mockResolvedValueOnce(json({ status: 'error', error: 'clone failed' }))
    await expect(settle(designCritiqueApi.discover('repo', 'x'))).rejects.toThrow('clone failed')
    fetchMock.mockResolvedValueOnce(json({ status: 'error' }))
    await expect(settle(designCritiqueApi.pollDiscover('j3'))).rejects.toThrow('that run did not finish')
  })

  it('keeps polling through a few lost replies, then gives up', async () => {
    fetchMock
      .mockResolvedValueOnce(json({ job: 'j4' }))
      .mockRejectedValueOnce(new TypeError('gateway restarting'))
      .mockRejectedValueOnce(new TypeError('gateway restarting'))
      .mockResolvedValueOnce(json({ status: 'done', result: { ok: true } }))
    await expect(settle(designCritiqueApi.discover('repo', 'y'))).resolves.toEqual({ ok: true })

    fetchMock.mockResolvedValueOnce(json({ job: 'j5' }))
    for (let i = 0; i < 8; i++) fetchMock.mockRejectedValueOnce(new TypeError('gateway down'))
    await expect(settle(designCritiqueApi.discover('repo', 'z'))).rejects.toThrow('gateway down')
  })
})

describe('designCritiqueApi render', () => {
  const body = { kind: 'url', value: 'https://x', handle: 'h', picks: [{ id: 'p1', label: 'Home' }] }

  it('posts the picks, reports the job, and resolves the rendered screens', async () => {
    const onJob = vi.fn()
    fetchMock
      .mockResolvedValueOnce(json({ job: 'r1' }))
      .mockResolvedValueOnce(json({ status: 'done', result: { screens: [{ step: 1, label: 'Home', path: '/p.png' }], couldNotSee: [] } }))
    const result = await settle(designCritiqueApi.render(body, onJob))
    expect(onJob).toHaveBeenCalledWith('r1')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(body)
    expect(result.screens).toEqual([{ step: 1, label: 'Home', path: '/p.png' }])
  })

  it('reconnects to a render job by id', async () => {
    fetchMock.mockResolvedValueOnce(json({ status: 'done', result: { screens: [], couldNotSee: ['Login'] } }))
    await expect(settle(designCritiqueApi.pollRender('r2'))).resolves.toEqual({ screens: [], couldNotSee: ['Login'] })
    expect(fetchMock.mock.calls[0][0]).toBe(`${DC}/render?job=r2`)
  })
})
