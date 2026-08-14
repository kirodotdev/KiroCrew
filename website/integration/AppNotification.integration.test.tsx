/**
 * Regression coverage for the macOS native-notification fix.
 *
 * Before the fix, App.tsx fired `new Notification(botName, { body: "N new
 * notification(s)" })` which discarded the real title/body from the backend
 * and surfaced as a generic "Kiro — 1 new notification" in Notification
 * Center.
 *
 * The fixed logic now lives in `src/hooks/useOSNotification.ts` and App.tsx
 * consumes it — so these tests exercise the real production hook via a thin
 * wrapper. The hook is event-driven (MC_NOTIFICATION_EVENT), the single
 * fan-out point every notification surface shares: each event carries its
 * own title/body/tag, so an aggregate "N new notifications" body cannot
 * exist by construction. Any regression in the hook will break these tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { createTestStore } from '../src/test/helpers'
import { useOSNotification } from '../src/hooks/useOSNotification'
import { MC_NOTIFICATION_EVENT, type McNotificationDetail } from '../src/hooks/notificationEvent'

const BOT_NAME = 'Kiro'
const AVATAR = 'https://example.test/avatar.png'

/** Thin wrapper that drives the real production hook. */
function NotificationHarness() {
  useOSNotification(BOT_NAME, AVATAR)
  return null
}

function fire(detail: McNotificationDetail) {
  act(() => {
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
  })
}

describe('useOSNotification (App integration)', () => {
  let notificationCtor: ReturnType<typeof vi.fn>
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    localStorage.clear()
    notificationCtor = vi.fn()
    // Stub the browser Notification global with a permission-granted mock.
    vi.stubGlobal(
      'Notification',
      Object.assign(notificationCtor, {
        permission: 'granted' as const,
        requestPermission: vi.fn(),
      }),
    )
    // Banners only fire while the tab is hidden — the in-app surfaces cover
    // the visible case.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  function mount(store: ReturnType<typeof createTestStore>) {
    act(() => {
      root.render(
        <Provider store={store}>
          <MemoryRouter>
            <NotificationHarness />
          </MemoryRouter>
        </Provider>,
      )
    })
  }

  it('forwards the backend title and body to the Notification constructor', () => {
    mount(createTestStore())

    fire({
      kind: 'approval',
      title: 'Approval needed',
      body: 'shell requires approval: ls /tmp',
      tag: 'appr-123',
    })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [title, opts] = notificationCtor.mock.calls[0]
    expect(title).toBe('Approval needed')
    expect(opts).toMatchObject({
      body: 'shell requires approval: ls /tmp',
      icon: AVATAR,
      tag: 'appr-123',
    })
    // The regression we are preventing: the old generic string.
    expect(opts.body).not.toMatch(/\d+ new notification/)
  })

  it('falls back to botName + generic body when the event lacks content', () => {
    mount(createTestStore())

    // Empty title and body simulate a malformed payload. Fallbacks kick in.
    fire({ kind: 'whatever', tag: 'job-xyz' })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [title, opts] = notificationCtor.mock.calls[0]
    expect(title).toBe(BOT_NAME)
    expect(opts.body).toBe('New notification')
    expect(opts.tag).toBe('job-xyz')
  })

  it('a burst keeps each banner with its own content — no aggregate body', () => {
    mount(createTestStore())

    fire({ kind: 'cron', title: 'A', body: 'first', tag: 'a-1' })
    fire({ kind: 'cron', title: 'B', body: 'second', tag: 'a-2' })
    fire({ kind: 'cron', title: 'C', body: 'third', tag: 'a-3' })

    expect(notificationCtor).toHaveBeenCalledTimes(3)
    for (const [title, opts] of notificationCtor.mock.calls) {
      expect(['A', 'B', 'C']).toContain(title)
      expect(opts.body).not.toMatch(/\d+ new notification/)
    }
  })

  it('uses a per-event tag so rapid updates replace instead of stack', () => {
    mount(createTestStore())

    fire({ kind: 'approval', title: 'A', body: 'first', tag: 'a-1' })
    fire({ kind: 'approval', title: 'B', body: 'second', tag: 'a-2' })

    expect(notificationCtor).toHaveBeenCalledTimes(2)
    expect(notificationCtor.mock.calls[0][1].tag).toBe('a-1')
    expect(notificationCtor.mock.calls[1][1].tag).toBe('a-2')
  })

  it('does not fire when permission is not granted', () => {
    // Replace the granted stub from beforeEach with a denied one, and capture
    // the *new* ctor — asserting on the stale `notificationCtor` from
    // beforeEach would always pass because the component no longer sees it.
    const deniedCtor = vi.fn()
    vi.stubGlobal(
      'Notification',
      Object.assign(deniedCtor, {
        permission: 'denied' as const,
        requestPermission: vi.fn(),
      }),
    )
    mount(createTestStore())

    fire({ kind: 'approval', title: 'A', body: 'first', tag: 'a-1' })

    expect(deniedCtor).not.toHaveBeenCalled()
  })

  it('does not fire while the tab is visible', () => {
    delete (document as { hidden?: boolean }).hidden
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    mount(createTestStore())

    fire({ kind: 'approval', title: 'A', body: 'first', tag: 'a-1' })

    expect(notificationCtor).not.toHaveBeenCalled()
  })
})
