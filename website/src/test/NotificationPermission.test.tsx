/**
 * System-notification permission surfaces: the Settings row (three states, one
 * action) and the bell popover's hint row (Allow / Not now).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render as rtlRender, fireEvent, screen, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import NotificationPermissionHint from '../components/notifications/NotificationPermissionHint'
import { PERMISSION_HINT_DISMISSED_KEY } from '../hooks/notificationBanner'

vi.mock('../hooks/useNotificationSound', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return { ...actual, playPreset: vi.fn() }
})

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** A stand-in for the platform global: static `permission`, and a
 *  `requestPermission` that flips it to whatever the test decided. */
function installNotification(initial: NotificationPermission, verdict: NotificationPermission = initial) {
  const requestPermission = vi.fn(async () => {
    FakeNotification.permission = verdict
    return verdict
  })
  class FakeNotification {
    static permission: NotificationPermission = initial
    static requestPermission = requestPermission
  }
  vi.stubGlobal('Notification', FakeNotification)
  return { requestPermission, FakeNotification }
}

beforeEach(() => {
  localStorage.clear()
  ;(window as unknown as { AudioContext: unknown }).AudioContext = vi.fn(() => ({
    state: 'running', currentTime: 0, destination: {}, resume: vi.fn(() => Promise.resolve()),
    createOscillator: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), type: '', frequency: { value: 0 }, onended: null })),
    createGain: vi.fn(() => ({ gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() })),
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Settings › Notifications › System notifications row', () => {
  it('granted: shows Allowed with no button', () => {
    installNotification('granted')
    render(<NotificationsPanel />)
    const row = screen.getByTestId('system-notifications-row')
    expect(row.textContent).toContain('Allowed')
    expect(row.querySelector('button')).toBeNull()
  })

  it('default: offers the Allow button, which asks the browser and re-reads the verdict', async () => {
    const { requestPermission } = installNotification('default', 'granted')
    render(<NotificationsPanel />)
    const btn = screen.getByRole('button', { name: 'Allow system notifications' })
    fireEvent.click(btn)
    expect(requestPermission).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByTestId('system-notifications-row').textContent).toContain('Allowed'))
    expect(screen.queryByRole('button', { name: 'Allow system notifications' })).toBeNull()
  })

  it('denied: plain-language pointer to the browser, no button', () => {
    installNotification('denied')
    render(<NotificationsPanel />)
    const row = screen.getByTestId('system-notifications-row')
    expect(row.textContent).toContain("Blocked in your browser. Turn it back on in the site's permission settings.")
    expect(row.querySelector('button')).toBeNull()
  })

  it('is absent where the platform has no Notification API', () => {
    vi.stubGlobal('Notification', undefined)
    render(<NotificationsPanel />)
    expect(screen.queryByTestId('system-notifications-row')).toBeNull()
  })

  it('re-reads the permission when the window regains focus', () => {
    const { FakeNotification } = installNotification('default')
    render(<NotificationsPanel />)
    expect(screen.getByRole('button', { name: 'Allow system notifications' })).toBeTruthy()
    FakeNotification.permission = 'granted'
    act(() => { window.dispatchEvent(new Event('focus')) })
    expect(screen.queryByRole('button', { name: 'Allow system notifications' })).toBeNull()
    expect(screen.getByTestId('system-notifications-row').textContent).toContain('Allowed')
  })

  it('has a banner toggle, default ON, persisting OFF to its own key', () => {
    installNotification('granted')
    render(<NotificationsPanel />)
    const toggle = screen.getByRole('switch', { name: /Show a banner for new notifications/i })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(toggle)
    expect(localStorage.getItem('mc-notification-banner')).toBe('0')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })
})

describe('Bell popover permission hint', () => {
  it('renders only while permission is default and there are notes', () => {
    installNotification('default')
    const { rerender } = rtlRender(<NotificationPermissionHint hasNotes={false} />)
    expect(screen.queryByTestId('notification-permission-hint')).toBeNull()
    rerender(<NotificationPermissionHint hasNotes />)
    expect(screen.getByTestId('notification-permission-hint')).toBeTruthy()
  })

  it('does not render once permission has a verdict', () => {
    installNotification('granted')
    rtlRender(<NotificationPermissionHint hasNotes />)
    expect(screen.queryByTestId('notification-permission-hint')).toBeNull()
  })

  it('Allow asks the browser and retires the row whatever the verdict', async () => {
    const { requestPermission } = installNotification('default', 'default')
    rtlRender(<NotificationPermissionHint hasNotes />)
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }))
    expect(requestPermission).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('notification-permission-hint')).toBeNull())
    expect(localStorage.getItem(PERMISSION_HINT_DISMISSED_KEY)).toBe('1')
  })

  it('a failed save keeps the row with a notice, and the next press retries', () => {
    installNotification('default')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('SecurityError') })
    rtlRender(<NotificationPermissionHint hasNotes />)
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.getByTestId('notification-permission-hint')).toBeTruthy()
    expect(screen.getByTestId('notification-permission-hint-save-failed').textContent).toContain("Couldn't save this. Try again.")
    expect(localStorage.getItem(PERMISSION_HINT_DISMISSED_KEY)).toBeNull()
    setItem.mockRestore()
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.queryByTestId('notification-permission-hint')).toBeNull()
    expect(localStorage.getItem(PERMISSION_HINT_DISMISSED_KEY)).toBe('1')
  })

  it('Not now retires the row permanently', () => {
    installNotification('default')
    const { unmount } = rtlRender(<NotificationPermissionHint hasNotes />)
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(screen.queryByTestId('notification-permission-hint')).toBeNull()
    expect(localStorage.getItem(PERMISSION_HINT_DISMISSED_KEY)).toBe('1')
    unmount()
    rtlRender(<NotificationPermissionHint hasNotes />)
    expect(screen.queryByTestId('notification-permission-hint')).toBeNull()
  })
})
