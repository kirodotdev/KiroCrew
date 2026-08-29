/**
 * Contract tests for ``WritingReviewPage`` — the app's page entry.
 *
 * The page is deliberately thin: it reads ``?review=<id>`` from the
 * URL, wraps children in ``AppApiProvider`` + ``WritingReviewProvider``,
 * and mounts the ``Workspace`` shell. Two branches to pin:
 *
 * 1. Without a ``?review`` query param the provider receives
 *    ``initialReviewId=null``.
 * 2. With ``?review=xyz`` the provider receives that value verbatim
 *    (a deep-link from a "review finished" notification).
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// Stub AppApiProvider — it depends on session/auth machinery the test
// does not need to reach. Capture the function-prop closures so we can
// invoke them directly and exercise the ``WritingReviewPage`` internal
// helpers (subscribeFn / navigateFn / notifyFn) for statement coverage.
const capturedAppApiProps = {
  subscribeFn: (() => () => {}) as (topic: string) => () => void,
  navigateFn: ((_path: string) => {}) as (path: string) => void,
  notifyFn: ((_message: string, _opts?: unknown) => {}) as (
    message: string,
    opts?: unknown,
  ) => void,
}
vi.mock('../../app-sdk', () => ({
  AppApiProvider: ({
    children,
    subscribeFn,
    navigateFn,
    notifyFn,
  }: {
    children: React.ReactNode
    subscribeFn: (topic: string) => () => void
    navigateFn: (path: string) => void
    notifyFn: (message: string, opts?: unknown) => void
  }) => {
    capturedAppApiProps.subscribeFn = subscribeFn
    capturedAppApiProps.navigateFn = navigateFn
    capturedAppApiProps.notifyFn = notifyFn
    return <>{children}</>
  },
}))

// Capture initialReviewId at construction so we can assert on it below.
const capturedProviderProps = {
  initialReviewId: undefined as string | null | undefined,
}
vi.mock('./context', () => ({
  WritingReviewProvider: ({
    initialReviewId,
    children,
  }: {
    initialReviewId: string | null
    children: React.ReactNode
  }) => {
    capturedProviderProps.initialReviewId = initialReviewId
    return <>{children}</>
  },
}))

vi.mock('./Workspace', () => ({
  default: () => <div data-testid="stub-workspace" />,
}))

import WritingReviewPage from './WritingReviewPage'

describe('WritingReviewPage', () => {
  it('passes initialReviewId=null when no ?review query param is present', () => {
    capturedProviderProps.initialReviewId = undefined
    render(
      <MemoryRouter initialEntries={['/writing-review']}>
        <WritingReviewPage />
      </MemoryRouter>,
    )
    expect(capturedProviderProps.initialReviewId).toBeNull()
  })

  it('forwards ?review=<id> to WritingReviewProvider verbatim', () => {
    capturedProviderProps.initialReviewId = undefined
    render(
      <MemoryRouter initialEntries={['/writing-review?review=abc-123']}>
        <WritingReviewPage />
      </MemoryRouter>,
    )
    expect(capturedProviderProps.initialReviewId).toBe('abc-123')
  })

  it('mounts the Workspace child under the providers', () => {
    render(
      <MemoryRouter initialEntries={['/writing-review']}>
        <WritingReviewPage />
      </MemoryRouter>,
    )
    expect(document.querySelector('[data-testid="stub-workspace"]')).not.toBeNull()
  })

  it('supplies subscribeFn / navigateFn / notifyFn to AppApiProvider that behave correctly when invoked', () => {
    render(
      <MemoryRouter initialEntries={['/writing-review']}>
        <WritingReviewPage />
      </MemoryRouter>,
    )
    // subscribeFn returns a no-op unsubscribe callable — this is what
    // AppApiProvider uses to register cross-app event listeners; the
    // page doesn't subscribe to any events so both the subscribe and
    // its unsubscribe MUST be safe to call.
    const unsubscribe = capturedAppApiProps.subscribeFn('any-topic')
    expect(typeof unsubscribe).toBe('function')
    expect(() => unsubscribe()).not.toThrow()

    // notifyFn dispatches a ``mc:notify`` CustomEvent so the dashboard
    // notification system can catch it. Invoke it and assert the event
    // fires with the message payload — the ``detail`` MUST include the
    // message the caller passed.
    const notifyListener = vi.fn()
    window.addEventListener('mc:notify', notifyListener as EventListener)
    try {
      capturedAppApiProps.notifyFn('review complete', { level: 'info' })
      expect(notifyListener).toHaveBeenCalledTimes(1)
      const dispatchedEvent = notifyListener.mock.calls[0][0] as CustomEvent
      expect(dispatchedEvent.detail).toMatchObject({ message: 'review complete' })
    } finally {
      window.removeEventListener('mc:notify', notifyListener as EventListener)
    }

    // navigateFn wraps react-router's ``navigate``; invoking with a
    // path should not throw. We already stubbed react-router's
    // navigate at MemoryRouter, so this exercises the wrapper path
    // without asserting on a specific navigation side-effect (the
    // stubbed navigate is a no-op).
    expect(() => capturedAppApiProps.navigateFn('/somewhere-else')).not.toThrow()
  })
})
