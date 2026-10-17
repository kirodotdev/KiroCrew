/**
 * Muting a notification channel must silence it on every attention surface,
 * per the backend contract in `kiro_crew/notifications/settings.py`
 * (`ChannelSettings.apply()`): a muted note arrives with `silenced: true` and
 * `priority: "passive"`, and every attention surface — badge count, sound,
 * native banner, feed styling — is meant to skip it.
 *
 * `useNativeNotification`'s unread count and latest-note selectors used to
 * filter only on `!acked`, so a muted note still incremented the count and
 * fired the native macOS banner even though the in-app feed correctly showed
 * it as "muted" (#11300). This pins the fix: neither selector may act on a
 * silenced / passive-priority note.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { addNotification } from '../store/notificationsSlice'
import type { Notification as AppNotification } from '../types'

class FakeNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  static instances: Array<{ title: string; body?: string }> = []
  title: string
  body?: string
  constructor(title: string, options?: { body?: string }) {
    this.title = title
    this.body = options?.body
    FakeNotification.instances.push({ title, body: options?.body })
  }
}

function mount() {
  const store = createTestStore()
  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store }, children)
  }
  renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })
  return store
}

describe('useNativeNotification skips silenced / passive-priority notes (#11300)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    FakeNotification.instances = []
    vi.stubGlobal('Notification', FakeNotification)
    // The banner fires only while the user is away from the window; the
    // muted/unmuted distinction under test needs a window that WOULD banner.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  it('does not fire a native banner for a note the backend marked silenced', () => {
    const store = mount()

    act(() => {
      store.dispatch(addNotification({
        kind: 'system',
        title: 'Host memory tight for 10 minutes',
        body: '',
        ts: '1.0',
        channel: 'system.resources',
        priority: 'passive',
        silenced: true,
      } as AppNotification))
    })

    expect(FakeNotification.instances).toHaveLength(0)
  })

  it('does not fire a native banner for a passive-priority note even when silenced is absent', () => {
    // The backend forces priority: "passive" on every muted note, but the
    // frontend selector must not depend on silenced being present at all --
    // priority alone is the same skip signal (ChannelSettings.apply()).
    const store = mount()

    act(() => {
      store.dispatch(addNotification({
        kind: 'system',
        title: 'Host memory pressure resolved',
        body: '',
        ts: '1.0',
        channel: 'system.resources',
        priority: 'passive',
      } as AppNotification))
    })

    expect(FakeNotification.instances).toHaveLength(0)
  })

  it('still fires a native banner for an ordinary, unmuted note', () => {
    const store = mount()

    act(() => {
      store.dispatch(addNotification({
        kind: 'approval',
        title: 'Tool approval',
        body: 'Bash',
        ts: '1.0',
        approval_id: 'ap-1',
      } as AppNotification))
    })

    expect(FakeNotification.instances).toHaveLength(1)
    expect(FakeNotification.instances[0].title).toBe('Tool approval')
  })

  it('a muted note does not become the latest-note title/body once an unmuted note follows', () => {
    // Regression for the OTHER half of the selector pair: latestNotif must
    // also skip silenced notes, or a muted note arriving after an unmuted one
    // could still leak its title/body into the toast text.
    const store = mount()

    act(() => {
      store.dispatch(addNotification({
        kind: 'approval',
        title: 'Tool approval',
        body: 'Bash',
        ts: '1.0',
        approval_id: 'ap-2',
      } as AppNotification))
    })
    act(() => {
      store.dispatch(addNotification({
        kind: 'system',
        title: 'Host memory tight for 10 minutes',
        body: '',
        ts: '2.0',
        channel: 'system.resources',
        priority: 'passive',
        silenced: true,
      } as AppNotification))
    })

    // Only the first (unmuted) note ever fired a banner; the muted second
    // note must not have re-fired one with its own title.
    expect(FakeNotification.instances).toHaveLength(1)
    expect(FakeNotification.instances[0].title).toBe('Tool approval')
  })
})
