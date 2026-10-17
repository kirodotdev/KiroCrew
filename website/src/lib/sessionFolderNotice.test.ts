import { afterEach, describe, expect, it, vi } from 'vitest'
import { SESSION_FOLDER_UNAVAILABLE_EVENT, takeSessionFolderNotice } from './sessionFolderNotice'
import { api } from '../api/client'

/* The create response's `session_folder_notice` is a one-off signal for that
 * create: it leaves the slot payload and is announced on `window`, so every
 * create path (the thunk and the direct client callers alike) reaches the App
 * shell's notice. */

function listen() {
  const seen = vi.fn()
  window.addEventListener(SESSION_FOLDER_UNAVAILABLE_EVENT, seen)
  return { seen, stop: () => window.removeEventListener(SESSION_FOLDER_UNAVAILABLE_EVENT, seen) }
}

describe('takeSessionFolderNotice', () => {
  it('strips the notice and announces it', () => {
    const { seen, stop } = listen()
    const slot = takeSessionFolderNotice({ key: 'NEW', project: '/ws', session_folder_notice: 'session_folder_unavailable' })
    stop()
    expect(slot).toEqual({ key: 'NEW', project: '/ws' })
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('passes a slot without a notice through unchanged and silently', () => {
    const { seen, stop } = listen()
    const created = { key: 'NEW', project: '/root/2026-10-12-abc123' }
    expect(takeSessionFolderNotice(created)).toBe(created)
    stop()
    expect(seen).not.toHaveBeenCalled()
  })
})

describe('api.createChatSlot', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('sends the opt-out and announces a notice from the response', async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ key: 'NEW', title: 'New', session_folder_notice: 'session_folder_unavailable' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const { seen, stop } = listen()

    const slot = await api.createChatSlot(undefined, undefined, undefined, undefined, 'persistent', undefined, undefined, undefined, undefined, true)
    stop()

    expect(slot).not.toHaveProperty('session_folder_notice')
    expect(seen).toHaveBeenCalledTimes(1)
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ memory_mode: 'persistent', no_session_folder: true })
  })
})
