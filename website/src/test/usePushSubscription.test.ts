import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
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

afterEach(() => {
  vi.useRealTimers()
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

  it('subscribe() refuses early without prompting when permission is already denied', async () => {
    const { pushManager } = installPushMocks(null)
    ;(Notification as unknown as { permission: string }).permission = 'denied'
    const { result } = renderHook(() => usePushSubscription())
    await act(async () => {
      await result.current.subscribe()
    })
    expect(Notification.requestPermission).not.toHaveBeenCalled()
    expect(pushManager.subscribe).not.toHaveBeenCalled()
    expect(api.vapidPublicKey).not.toHaveBeenCalled()
    expect(api.subscribePush).not.toHaveBeenCalled()
    expect(result.current.busy).toBe(false)
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

  it('subscribe() rolls back the browser subscription when the server call fails', async () => {
    const { subscription, pushManager } = installPushMocks(null)
    // Stateful browser side: subscribe() creates the subscription, its
    // unsubscribe() removes it, and getSubscription() reads what is left.
    let live = false
    pushManager.subscribe.mockImplementation(() => {
      live = true
      return Promise.resolve(subscription)
    })
    subscription.unsubscribe.mockImplementation(() => {
      live = false
      return Promise.resolve(true)
    })
    pushManager.getSubscription.mockImplementation(() => Promise.resolve(live ? subscription : null))
    ;(api.subscribePush as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('store write failed'))

    const first = renderHook(() => usePushSubscription())
    await act(async () => {
      await first.result.current.subscribe()
    })
    expect(subscription.unsubscribe).toHaveBeenCalled()
    expect(first.result.current.subscribed).toBe(false)
    expect(first.result.current.error).toMatch(/turn on notifications/i)
    first.unmount()

    // Reopening Settings must not show the toggle on for a device the
    // gateway never registered.
    const readsBefore = pushManager.getSubscription.mock.calls.length
    const second = renderHook(() => usePushSubscription())
    await waitFor(() =>
      expect(pushManager.getSubscription.mock.calls.length).toBeGreaterThan(readsBefore),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(second.result.current.subscribed).toBe(false)
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

  it('unsubscribe() keeps the gateway registration when the browser unsubscribe fails', async () => {
    const { subscription } = installPushMocks({ endpoint: 'https://push.example.com/x' })
    subscription.unsubscribe.mockRejectedValueOnce(new Error('push service unreachable'))
    const { result } = renderHook(() => usePushSubscription())
    await waitFor(() => expect(result.current.subscribed).toBe(true))
    await act(async () => {
      await result.current.unsubscribe()
    })
    // The browser still holds the subscription, so the gateway must too:
    // otherwise the toggle reads on after a reload while nothing is delivered.
    expect(api.unsubscribePush).not.toHaveBeenCalled()
    expect(result.current.subscribed).toBe(true)
    expect(result.current.error).not.toBeNull()
  })

  it('unsubscribe() drops the browser subscription before the gateway one', async () => {
    const { subscription } = installPushMocks({ endpoint: 'https://push.example.com/x' })
    const order: string[] = []
    subscription.unsubscribe.mockImplementation(() => {
      order.push('browser')
      return Promise.resolve(true)
    })
    ;(api.unsubscribePush as ReturnType<typeof vi.fn>).mockImplementationOnce(() => {
      order.push('gateway')
      return Promise.reject(new Error('gateway down'))
    })
    const { result } = renderHook(() => usePushSubscription())
    await waitFor(() => expect(result.current.subscribed).toBe(true))
    await act(async () => {
      await result.current.unsubscribe()
    })
    // A gateway failure after the browser side is gone still reads as off.
    expect(order).toEqual(['browser', 'gateway'])
    expect(result.current.subscribed).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('surfaces an error when the mount-time subscription read fails', async () => {
    installPushMocks(null)
    // Make the mount-effect getSubscription read reject.
    ;(navigator as unknown as { serviceWorker: { ready: Promise<unknown> } }).serviceWorker = {
      ready: Promise.resolve({
        pushManager: { getSubscription: vi.fn(() => Promise.reject(new Error('sw read boom'))) },
      }),
    }
    const { result } = renderHook(() => usePushSubscription())
    await waitFor(() => expect(result.current.error).toMatch(/turn on notifications/i))
    expect(result.current.subscribed).toBe(false)
  })

  it('subscribe() gives up with an error when the service worker never becomes ready', async () => {
    installPushMocks(null)
    ;(navigator as unknown as { serviceWorker: { ready: Promise<unknown> } }).serviceWorker = {
      ready: new Promise(() => {}),
    }
    const { result } = renderHook(() => usePushSubscription())
    vi.useFakeTimers()
    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.subscribe()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_500)
      await pending
    })
    expect(api.subscribePush).not.toHaveBeenCalled()
    expect(result.current.busy).toBe(false)
    expect(result.current.error).toMatch(/turn on notifications/i)
  })

  it('reports unsupported when the Push API is absent', () => {
    delete (navigator as unknown as { serviceWorker?: unknown }).serviceWorker
    delete (globalThis as unknown as { PushManager?: unknown }).PushManager
    const { result } = renderHook(() => usePushSubscription())
    expect(result.current.supported).toBe(false)
  })
})
