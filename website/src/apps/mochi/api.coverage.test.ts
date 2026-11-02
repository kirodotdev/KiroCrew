/**
 * Mochi API client: the fetch wrapper for the Mochi backend.
 *
 * fetch is the seam. The tests assert the URL, method, headers and body each call sends,
 * the parsed result, the enabled/disabled/starting probe mapping, and the error paths.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  enableMochi,
  getActivity,
  getMochiVersion,
  getPetState,
  getPinned,
  getPlan,
  getSettings,
  getSoul,
  getStats,
  getWatchlist,
  markPinnedSeen,
  probeEnabled,
  unpinFile,
  updateSettings,
  updateWatchlist,
} from './api'

const API = '/api/apps/mochi'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('GET readers', () => {
  it.each([
    ['getWatchlist', () => getWatchlist(), '/watchlist', { items: [] }],
    ['getStats', () => getStats(), '/stats', { streak: 3 }],
    ['getPinned', () => getPinned(), '/pinned', { pins: [] }],
    ['getSoul', () => getSoul(), '/soul', { soul: 's', petName: 'Mochi', isDefault: true }],
    ['getPetState', () => getPetState(), '/pet-state', { state: 'idle', mood: 'calm' }],
    ['getPlan', () => getPlan(), '/plan', { tasks: [], narrative: 'quiet day' }],
    ['getActivity', () => getActivity(), '/activity', { entries: [] }],
    ['getSettings', () => getSettings(), '/settings', { mode: 'quiet' }],
  ])('%s reads the app path with same-origin credentials and returns the body', async (_name, call, path, body) => {
    fetchMock.mockResolvedValue(json(body))

    await expect(call()).resolves.toEqual(body)

    expect(fetchMock).toHaveBeenCalledWith(`${API}${path}`, { credentials: 'same-origin' })
  })

  it('maps a refusal from a reader to an error carrying its status', async () => {
    fetchMock.mockResolvedValue(json({ error: 'Mochi is off' }, 500))

    await expect(getStats()).rejects.toMatchObject({ status: 500, message: 'Mochi is off' })
  })
})

describe('POST writers', () => {
  it('updateWatchlist posts the whole params object as JSON', async () => {
    const result = { updated: true, items: [] }
    fetchMock.mockResolvedValue(json(result))
    const params = { cancel: ['w1'], remove: ['w2'] }

    await expect(updateWatchlist(params)).resolves.toEqual(result)

    expect(fetchMock).toHaveBeenCalledWith(`${API}/watchlist/update`, {
      credentials: 'same-origin',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    })
  })

  it('unpinFile posts the file path to the unpin route', async () => {
    fetchMock.mockResolvedValue(json({ ok: true }))

    await expect(unpinFile('/work/notes.md')).resolves.toEqual({ ok: true })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${API}/pinned/unpin`)
    expect((init as RequestInit).body).toBe(JSON.stringify({ path: '/work/notes.md' }))
  })

  it('markPinnedSeen posts the file path to the mark-seen route', async () => {
    fetchMock.mockResolvedValue(json({ ok: true }))

    await expect(markPinnedSeen('/work/todo.md')).resolves.toEqual({ ok: true })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${API}/pinned/mark-seen`)
    expect((init as RequestInit).body).toBe(JSON.stringify({ path: '/work/todo.md' }))
  })

  it('updateSettings posts the patch to the settings route and returns the stored settings', async () => {
    fetchMock.mockResolvedValue(json({ mode: 'active' }))

    await expect(updateSettings({ mode: 'active' })).resolves.toEqual({ mode: 'active' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${API}/settings`)
    expect(init).toMatchObject({ method: 'POST', credentials: 'same-origin' })
    expect((init as RequestInit).body).toBe(JSON.stringify({ mode: 'active' }))
  })
})

describe('probeEnabled', () => {
  it('reports enabled when the pet-state route answers 2xx', async () => {
    fetchMock.mockResolvedValue(json({ state: 'idle', mood: 'calm' }))

    await expect(probeEnabled()).resolves.toBe('enabled')
    expect(fetchMock).toHaveBeenCalledWith(`${API}/pet-state`, { credentials: 'same-origin' })
  })

  it('reports disabled, not an error, on a 403', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 403 }))

    await expect(probeEnabled()).resolves.toBe('disabled')
  })

  it('reports starting, not an error, on a 503', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 503 }))

    await expect(probeEnabled()).resolves.toBe('starting')
  })

  it('throws on any other failure so an outage is not shown as sleeping', async () => {
    fetchMock.mockResolvedValue(json({ error: 'boom' }, 500))

    await expect(probeEnabled()).rejects.toMatchObject({ status: 500, message: 'boom' })
  })
})

describe('enableMochi', () => {
  it('posts to the core enable route with same-origin credentials and resolves', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 200 }))

    await expect(enableMochi()).resolves.toBeUndefined()

    expect(fetchMock).toHaveBeenCalledWith('/api/apps/mochi/enable', {
      method: 'POST',
      credentials: 'same-origin',
    })
  })

  it('throws the refusal when the enable call is rejected', async () => {
    fetchMock.mockResolvedValue(json({ error: 'not allowed' }, 403))

    await expect(enableMochi()).rejects.toMatchObject({ status: 403, message: 'not allowed' })
  })
})

describe('getMochiVersion', () => {
  it('reads a top-level version string from the manifest route', async () => {
    fetchMock.mockResolvedValue(json({ version: '1.4.2' }))

    await expect(getMochiVersion()).resolves.toBe('1.4.2')
    expect(fetchMock).toHaveBeenCalledWith(`${API}/manifest`, { credentials: 'same-origin' })
  })

  it('falls back to the version nested under manifest, and returns empty when none is a string', async () => {
    fetchMock.mockResolvedValueOnce(json({ manifest: { version: '2.0.0' } }))
    fetchMock.mockResolvedValueOnce(json({ version: 3 }))

    await expect(getMochiVersion()).resolves.toBe('2.0.0')
    await expect(getMochiVersion()).resolves.toBe('')
  })

  it('returns an empty string when the manifest route refuses', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 404 }))

    await expect(getMochiVersion()).resolves.toBe('')
  })
})
