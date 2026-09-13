import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useNativeNotification } from '../hooks/useNativeNotification'

// The hook reads two values off the store via useAppSelector. Mock the module
// so each test drives notifCount / latestNotif directly and exercises the
// notification-delivery branches (SW registration, constructor fallback,
// permission request) without standing up the full Redux tree.
let notifCount = 0
let latestNotif: Record<string, unknown> | null = null

vi.mock('../store', () => ({
  useAppSelector: (sel: (s: unknown) => unknown) => {
    const state = {
      notifications: {
        items: Array.from({ length: notifCount }, (_, i) => ({
          acked: false,
          ...(latestNotif && i === notifCount - 1 ? latestNotif : {}),
        })),
      },
    }
    return sel(state)
  },
}))

type NavLike = { serviceWorker?: unknown }

function setNotification(permission: NotificationPermission, requestPermission = vi.fn()) {
  ;(globalThis as unknown as { Notification: unknown }).Notification = Object.assign(
    vi.fn(),
    { permission, requestPermission },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  notifCount = 0
  latestNotif = null
  delete (navigator as unknown as NavLike).serviceWorker
})

afterEach(() => {
  delete (globalThis as unknown as { Notification?: unknown }).Notification
})

describe('useNativeNotification', () => {
  it('delivers through the service worker registration when permission is granted', async () => {
    const showNotification = vi.fn(() => Promise.resolve())
    ;(navigator as unknown as NavLike).serviceWorker = {
      ready: Promise.resolve({ showNotification }),
    }
    setNotification('granted')
    notifCount = 1
    latestNotif = { title: 'Deploy done', body: 'prod is live', job_id: 'j-1' }

    renderHook(() => useNativeNotification('Kiro', '/avatar.png'))

    await waitFor(() =>
      expect(showNotification).toHaveBeenCalledWith('Deploy done', {
        body: 'prod is live',
        icon: '/avatar.png',
        tag: 'j-1',
      }),
    )
    // The constructor must NOT be used when the SW path resolves.
    expect(
      (globalThis as unknown as { Notification: ReturnType<typeof vi.fn> }).Notification,
    ).not.toHaveBeenCalled()
  })

  it('falls back to the Notification constructor when the SW registration rejects', async () => {
    ;(navigator as unknown as NavLike).serviceWorker = {
      ready: Promise.reject(new Error('no active registration')),
    }
    setNotification('granted')
    notifCount = 2 // delta > 1 -> plural default body
    latestNotif = null

    renderHook(() => useNativeNotification('Kiro', '/avatar.png'))

    const Ctor = (globalThis as unknown as { Notification: ReturnType<typeof vi.fn> }).Notification
    await waitFor(() =>
      expect(Ctor).toHaveBeenCalledWith('Kiro', {
        body: '2 new notifications',
        icon: '/avatar.png',
        tag: 'kirocrew-notif',
      }),
    )
  })

  it('uses the constructor directly when serviceWorker is unavailable', () => {
    // no navigator.serviceWorker
    setNotification('granted')
    notifCount = 1
    latestNotif = { title: 'Hi', body: 'there' }

    renderHook(() => useNativeNotification('Kiro', '/avatar.png'))

    const Ctor = (globalThis as unknown as { Notification: ReturnType<typeof vi.fn> }).Notification
    expect(Ctor).toHaveBeenCalledWith('Hi', {
      body: 'there',
      icon: '/avatar.png',
      tag: 'kirocrew-notif',
    })
  })

  it('swallows an unsupported-platform throw from the constructor', () => {
    setNotification('granted')
    ;(globalThis as unknown as { Notification: unknown }).Notification = Object.assign(
      vi.fn(() => {
        throw new Error('Illegal constructor')
      }),
      { permission: 'granted' as NotificationPermission, requestPermission: vi.fn() },
    )
    notifCount = 1

    expect(() =>
      renderHook(() => useNativeNotification('Kiro', '/avatar.png')),
    ).not.toThrow()
  })

  it('requests permission when it is still default and does not notify', () => {
    const requestPermission = vi.fn(() => Promise.resolve('granted' as NotificationPermission))
    setNotification('default', requestPermission)
    notifCount = 1

    renderHook(() => useNativeNotification('Kiro', '/avatar.png'))

    expect(requestPermission).toHaveBeenCalledTimes(1)
  })

  it('does nothing when there is no new notification', () => {
    const showNotification = vi.fn()
    ;(navigator as unknown as NavLike).serviceWorker = {
      ready: Promise.resolve({ showNotification }),
    }
    setNotification('granted')
    notifCount = 0

    renderHook(() => useNativeNotification('Kiro', '/avatar.png'))

    expect(showNotification).not.toHaveBeenCalled()
  })
})
