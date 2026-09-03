import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { usePushSubscription } from '../hooks/usePushSubscription'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    vapidPublicKey: vi.fn(() => Promise.resolve({ publicKey: 'BExamplePublicKeyBase64Url' })),
    subscribePush: vi.fn(() => Promise.resolve({ ok: true, endpoint: 'https://push/x' })),
    unsubscribePush: vi.fn(() => Promise.resolve({ ok: true, removed: true })),
  },
}))

function installPushMocks(existing: { endpoint: string } | null) {
  const subscription = {
    endpoint: 'https://push.example.com/x',
    toJSON: () => ({ endpoint: 'https://push.example.com/x', keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn(() => Promise.resolve(true)),
  }
  const pushManager = {
    getSubscription: vi.fn(() => Promise.resolve(existing ? subscription : null)),
    subscribe: vi.fn(() => Promise.resolve(subscription)),
  }
  ;(navigator as unknown as { serviceWorker: unknown }).serviceWorker = {
    ready: Promise.resolve({ pushManager }),
  }
  ;(globalThis as unknown as { PushManager: unknown }).PushManager = function () {}
  ;(globalThis as unknown as { Notification: unknown }).Notification = {
    requestPermission: vi.fn(() => Promise.resolve('granted')),
  }
  return { subscription, pushManager }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('usePushSubscription', () => {
  it('reports supported when serviceWorker + PushManager exist', async () => {
    installPushMocks(null)
    const { result } = renderHook(() => usePushSubscription())
    expect(result.current.supported).toBe(true)
    await waitFor(() => expect(result.current.subscribed).toBe(false))
  })

  it('subscribe() requests permission, subscribes, and posts to the API', async () => {
    const { pushManager } = installPushMocks(null)
    const { result } = renderHook(() => usePushSubscription())
    await act(async () => {
      await result.current.subscribe()
    })
    expect(Notification.requestPermission).toHaveBeenCalled()
    expect(pushManager.subscribe).toHaveBeenCalled()
    expect(api.subscribePush).toHaveBeenCalledOnce()
    expect(result.current.subscribed).toBe(true)
  })

  it('subscribe() surfaces an error and does not subscribe when permission is denied', async () => {
    installPushMocks(null)
    ;(Notification.requestPermission as ReturnType<typeof vi.fn>).mockResolvedValueOnce('denied')
    const { result } = renderHook(() => usePushSubscription())
    await act(async () => {
      await result.current.subscribe()
    })
    expect(api.subscribePush).not.toHaveBeenCalled()
    expect(result.current.subscribed).toBe(false)
    expect(result.current.error).toMatch(/blocked/i)
  })

  it('subscribe() re-subscribes when the existing key no longer matches the server VAPID key', async () => {
    const { subscription, pushManager } = installPushMocks({ endpoint: 'https://push.example.com/x' })
    // Give the existing subscription an applicationServerKey that differs from
    // the one urlBase64ToArrayBuffer('BExamplePublicKeyBase64Url') produces, so
    // the rotation-mismatch branch fires.
    ;(subscription as unknown as { options: unknown }).options = {
      applicationServerKey: new Uint8Array([1, 2, 3]).buffer,
    }
    const { result } = renderHook(() => usePushSubscription())
    await act(async () => {
      await result.current.subscribe()
    })
    // Stale sub dropped, fresh one created, server told about the new one.
    expect(subscription.unsubscribe).toHaveBeenCalled()
    expect(pushManager.subscribe).toHaveBeenCalled()
    expect(api.subscribePush).toHaveBeenCalledOnce()
    expect(result.current.subscribed).toBe(true)
  })

  it('unsubscribe() unsubscribes locally and calls the API', async () => {
    const { subscription } = installPushMocks({ endpoint: 'https://push.example.com/x' })
    const { result } = renderHook(() => usePushSubscription())
    await waitFor(() => expect(result.current.subscribed).toBe(true))
    await act(async () => {
      await result.current.unsubscribe()
    })
    expect(api.unsubscribePush).toHaveBeenCalledWith('https://push.example.com/x')
    expect(subscription.unsubscribe).toHaveBeenCalled()
    expect(result.current.subscribed).toBe(false)
  })

  it('reports unsupported when the Push API is absent', () => {
    delete (navigator as unknown as { serviceWorker?: unknown }).serviceWorker
    delete (globalThis as unknown as { PushManager?: unknown }).PushManager
    const { result } = renderHook(() => usePushSubscription())
    expect(result.current.supported).toBe(false)
  })
})
