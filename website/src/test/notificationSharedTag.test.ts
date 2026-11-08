import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { renderHook, waitFor } from '@testing-library/react'
import { useNativeNotification } from '../hooks/useNativeNotification'

// One delivered note can raise two OS banners while the dashboard tab is open
// but hidden: the tab's own toast (useNativeNotification) and the service
// worker's Web Push banner. The OS collapses banners only when their tags are
// equal, so both paths must use the tag the gateway stamps on the note
// (`native_notification_tag`). These tests run the REAL hook and the REAL
// sw.js push handler against the same note and compare the tags they post.

let notifCount = 0
let latestNotif: Record<string, unknown> | null = null

vi.mock('../store', () => ({
  useAppSelector: (sel: (s: unknown) => unknown) =>
    sel({
      notifications: {
        items: Array.from({ length: notifCount }, (_, i) => ({
          acked: false,
          ...(latestNotif && i === notifCount - 1 ? latestNotif : {}),
        })),
      },
    }),
}))

const SW_PATH = path.resolve(__dirname, '..', '..', 'public', 'sw.js')
type NavLike = { serviceWorker?: unknown }

/** Run sw.js's push handler for `notification` with no visible tab; return the tag it shows. */
async function workerTag(notification: Record<string, unknown>): Promise<string | undefined> {
  const listeners: Record<string, (e: unknown) => void> = {}
  const showNotification = vi.fn((_title: string, _opts: { tag?: string }) => Promise.resolve())
  const fakeSelf = {
    addEventListener: (kind: string, fn: (e: unknown) => void) => { listeners[kind] = fn },
    location: { origin: 'https://dash.example' },
    skipWaiting: () => {},
    clients: { claim: () => {}, matchAll: async () => [{ visibilityState: 'hidden' }] },
    registration: { showNotification },
  }
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(
    fakeSelf, { open: async () => ({ addAll: async () => {} }), keys: async () => [] }, () => Promise.resolve({}),
  )
  let pending: Promise<unknown> = Promise.resolve()
  listeners.push({
    data: { json: () => ({ web_push: 8030, notification }) },
    waitUntil: (p: Promise<unknown>) => { pending = p },
  })
  await pending
  expect(showNotification).toHaveBeenCalledTimes(1)
  return showNotification.mock.calls[0][1].tag
}

/** Run the tab hook for `note` while the window is away; return the tag it posts. */
async function tabTag(note: Record<string, unknown>): Promise<string | undefined> {
  const showNotification = vi.fn((_title: string, _opts: { tag?: string }) => Promise.resolve())
  ;(navigator as unknown as NavLike).serviceWorker = { ready: Promise.resolve({ showNotification }) }
  notifCount = 1
  latestNotif = note
  const { unmount } = renderHook(() => useNativeNotification('Kiro', '/avatar.png'))
  await waitFor(() => expect(showNotification).toHaveBeenCalledTimes(1))
  unmount()
  return showNotification.mock.calls[0][1].tag
}

beforeEach(() => {
  vi.clearAllMocks()
  notifCount = 0
  latestNotif = null
  vi.spyOn(document, 'hasFocus').mockReturnValue(false)
  ;(globalThis as unknown as { Notification: unknown }).Notification = Object.assign(vi.fn(), {
    permission: 'granted' as NotificationPermission,
    requestPermission: vi.fn(),
  })
})

afterEach(() => {
  delete (navigator as unknown as NavLike).serviceWorker
  delete (globalThis as unknown as { Notification?: unknown }).Notification
})

describe('one tag for the tab toast and the Web Push banner', () => {
  it('posts the identical tag from both paths for the same event', async () => {
    // A gateway note: the WebSocket copy and the push payload carry the same
    // stamped tag (asserted on the Python side), and a job id that differs
    // from it, so a tab that fell back to its own id chain would fail here.
    const stamped = 'job_id:j-42'
    const tab = await tabTag({ title: 'Deploy done', body: 'b', job_id: 'j-42', ts: '1.5', tag: stamped })
    const worker = await workerTag({ title: 'Deploy done', body: 'b', tag: stamped })
    expect(tab).toBe(stamped)
    expect(worker).toBe(tab)
  })

  it('keeps the push deep link on the tab banner that replaces it', async () => {
    const showNotification = vi.fn((_title: string, _opts: { data?: { url?: string } }) => Promise.resolve())
    ;(navigator as unknown as NavLike).serviceWorker = { ready: Promise.resolve({ showNotification }) }
    notifCount = 1
    latestNotif = { title: 'Deploy done', body: 'b', tag: 'job_id:j-7', url: '/chat/abc' }
    const { unmount } = renderHook(() => useNativeNotification('Kiro', '/avatar.png'))
    await waitFor(() => expect(showNotification).toHaveBeenCalledTimes(1))
    unmount()
    expect(showNotification.mock.calls[0][1].data).toEqual({ url: '/chat/abc' })
  })

  it('gives distinct events distinct tags on both paths', async () => {
    const a = 'ts:1700000000.1'
    const b = 'ts:1700000000.2'
    const tabA = await tabTag({ title: 'x', body: 'b', ts: '1700000000.1', tag: a })
    const tabB = await tabTag({ title: 'x', body: 'b', ts: '1700000000.2', tag: b })
    const swA = await workerTag({ title: 'x', body: 'b', tag: a })
    const swB = await workerTag({ title: 'x', body: 'b', tag: b })
    expect(tabA).not.toBe(tabB)
    expect(swA).not.toBe(swB)
    expect([tabA, tabB]).toEqual([swA, swB])
  })
})
