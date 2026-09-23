/**
 * Regression coverage for the macOS native-notification fix.
 *
 * Before the fix, App.tsx fired `new Notification(botName, { body: "N new
 * notification(s)" })` which discarded the real title/body from the backend
 * and surfaced as a generic "Kiro — 1 new notification" in Notification
 * Center.
 *
 * The fixed logic now lives in `src/hooks/useNativeNotification.ts` and
 * `App.tsx` consumes it — so these tests exercise the real production hook
 * via a thin wrapper. Any regression in the hook will break these tests.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act } from '@testing-library/react'
import { Provider } from 'react-redux'
import React from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { createTestStore } from '../src/test/helpers'
import { useNativeNotification } from '../src/hooks/useNativeNotification'
import { approvalNotificationBody } from '../src/lib/approvalNotificationBody'
import type { Notification as MeshNotification } from '../src/types'

const BOT_NAME = 'Kiro'
const AVATAR = 'https://example.test/avatar.png'

/** Thin wrapper that drives the real production hook. */
function NotificationHarness() {
  useNativeNotification(BOT_NAME, AVATAR)
  return null
}

function makeNotif(overrides: Partial<MeshNotification> = {}): MeshNotification {
  return {
    kind: 'approval',
    title: 'Default title',
    body: 'Default body',
    ts: '2026-04-29T21:00:00Z',
    acked: false,
    ...overrides,
  }
}

describe('useNativeNotification', () => {
  let notificationCtor: ReturnType<typeof vi.fn>
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    notificationCtor = vi.fn()
    // Stub the browser Notification global with a permission-granted mock.
    vi.stubGlobal(
      'Notification',
      Object.assign(notificationCtor, {
        permission: 'granted' as const,
        requestPermission: vi.fn(),
      }),
    )
    // The toast fires only while the user is away from the window (the in-app
    // banner covers a focused one); happy-dom's default is the focused state,
    // so hide the document for every formatting case below.
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
          <NotificationHarness />
        </Provider>,
      )
    })
  }

  it('forwards the backend title and body to the Notification constructor', () => {
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    // Push a real notification into the store. This is what makes notifCount
    // go from 0 → 1 and should fire the effect.
    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({
          title: 'Approval needed',
          body: 'shell requires approval: ls /tmp',
          approval_id: 'appr-123',
        }),
      })
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

  it('falls back to botName + generic body when the notification lacks content', () => {
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        // Empty title and body simulate a malformed payload. Fallbacks kick in.
        payload: makeNotif({ title: '', body: '', job_id: 'job-xyz' }),
      })
    })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [title, opts] = notificationCtor.mock.calls[0]
    expect(title).toBe(BOT_NAME)
    expect(opts.body).toBe('New notification')
    expect(opts.tag).toBe('job-xyz')
  })

  it('uses the delta (not total) when body falls back on a later burst', () => {
    // Seed the store with 3 already-unacked, body-less notifications so the
    // hook's `prev` ref reaches 3. Then drop a single new body-less one —
    // the fallback should say "1 new notification", not "4 new notifications".
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      for (let i = 0; i < 3; i++) {
        store.dispatch({
          type: 'notifications/addNotification',
          payload: makeNotif({
            title: '',
            body: '',
            approval_id: `seed-${i}`,
            // Reducer dedupes by `ts`, so each seed needs a distinct timestamp.
            ts: `2026-04-29T21:00:0${i}Z`,
          }),
        })
      }
    })
    // Clear the calls from the initial burst — we only care about the next one.
    notificationCtor.mockClear()

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({
          title: '',
          body: '',
          approval_id: 'late-1',
          ts: '2026-04-29T21:00:10Z',
        }),
      })
    })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [, opts] = notificationCtor.mock.calls[0]
    // Regression: old code used notifCount (4), new code uses delta (1).
    expect(opts.body).toBe('New notification')
    expect(opts.body).not.toMatch(/4 new/)
  })

  it('flattens the markdown body -- an OS toast renders plain text only', () => {
    // A note body is markdown by contract (the detail panel renders it as
    // markdown). macOS Notification Center does not: before the fix it painted
    // `**auto/verify-python-package**` with the asterisks visible.
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({
          kind: 'skills',
          title: 'New skill awaiting review',
          body:
            '**auto/verify-python-package-contains-source** — Verify a built wheel\n' +
            '\nGenerated from a session. Needs your approval before it can be used.' +
            '\n**Triggers:** after a wheel build' +
            '\n_Bundles executable scripts — review them before approving._',
          approval_id: 'skill-1',
        }),
      })
    })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [, opts] = notificationCtor.mock.calls[0]
    expect(opts.body).not.toMatch(/[*_`#>]/)
    expect(opts.body).toContain('auto/verify-python-package-contains-source')
    expect(opts.body).toContain('Triggers: after a wheel build')
    // The blank line between the heading and the description survives as a
    // visible separator rather than running the two paragraphs together.
    expect(opts.body).toContain('Verify a built wheel · Generated from a session')
  })

  it.each([
    'rm -rf *cache*', '_tmp_', '~~name~~', '~/.ssh/id_rsa', 'echo hi > out.log',
    'echo ```; rm -rf *cache*', 'echo ````; rm -rf *cache*',
  ])('sends the exact authorized command to the native banner: %s', command => {
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)
    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({
          body: approvalNotificationBody('agent', command, 'Review this'),
          approval_id: 'literal-command',
        }),
      })
    })
    expect(notificationCtor).toHaveBeenCalledTimes(1)
    expect(notificationCtor.mock.calls[0][1].body).toBe(`Source: agent · ${command} · Review this`)
  })

  it('falls back to the generic body when the markdown flattens to nothing', () => {
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({ title: 'Heads up', body: '**__**', job_id: 'job-md' }),
      })
    })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [, opts] = notificationCtor.mock.calls[0]
    expect(opts.body).toBe('New notification')
  })

  it('falls back to the generic body when a persisted body is not a string', () => {
    // A legacy or corrupted persisted row: truthy, so the `body ?` branch is
    // taken, but not text. The banner must degrade to the fallback line, not
    // throw out of the granted-notification effect.
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({ title: 'Heads up', body: { raw: 42 } as unknown as string, job_id: 'job-bad' }),
      })
    })

    expect(notificationCtor).toHaveBeenCalledTimes(1)
    const [, opts] = notificationCtor.mock.calls[0]
    expect(opts.body).toBe('New notification')
  })

  it('uses a per-event tag so rapid updates replace instead of stack', () => {
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({
          title: 'A',
          body: 'first',
          approval_id: 'a-1',
          ts: '2026-04-29T21:00:00Z',
        }),
      })
    })
    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif({
          title: 'B',
          body: 'second',
          approval_id: 'a-2',
          ts: '2026-04-29T21:00:01Z',
        }),
      })
    })

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
    const store = createTestStore({ notifications: { items: [] } as any })
    mount(store)

    act(() => {
      store.dispatch({
        type: 'notifications/addNotification',
        payload: makeNotif(),
      })
    })

    expect(deniedCtor).not.toHaveBeenCalled()
  })
})
