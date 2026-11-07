import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { FRAME_ALIVE_MESSAGE_TYPE, FRAME_PING_MESSAGE_TYPE } from '../../lib/widgetSrcdoc'
import { FRAME_ALIVE_MAX_RECOVERIES, FRAME_ALIVE_TIMEOUT_MS } from '../../hooks/useFrameAliveRecovery'

/**
 * A FRAME THE BROWSER RELOADS FINDS ITS URL SPENT.
 *
 * The sandbox-doc url is single-use, so a reload the browser does on its own (a
 * gateway restart, a restored remote connection, a woken tab) gets a 404 and the
 * tab goes white. The tab pings the frame on every `load`; the document's host
 * script answers, a 404 cannot, and no answer means a new url is minted.
 */

const memberDashboard = vi.fn()
const retry = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: { memberDashboard: (...a: unknown[]) => memberDashboard(...a) },
  }
})

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? 'https://localhost/sandbox/doc' : null,
    failed: false,
    pending: false,
    stalled: false,
    retry,
  }),
}))

import CrewDynamicDashboard from './CrewDynamicDashboard'

const answer = (scripts?: boolean) => ({
  slug: 'atlas',
  member: 'Atlas',
  dashboard: 'page-one',
  state: 'ok',
  html: '<p>stored</p>',
  rendered_html: '<p>page</p>',
  manifest: { fields: {} },
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  ...(scripts === undefined ? {} : { scripts }),
})

async function mount(scripts?: boolean) {
  memberDashboard.mockResolvedValue(answer(scripts))
  const { container } = renderWithProviders(
    <CrewDynamicDashboard target={{ kind: 'member', slug: 'atlas', member: 'Atlas' }} displayName="Atlas" />,
  )
  const frame = await waitFor(() => {
    const found = container.querySelector('[data-testid="crew-dashboard-iframe"]')
    if (!found) throw new Error('the frame never mounted')
    return found as HTMLIFrameElement
  })
  // Flush any React effects that are still pending after waitFor returns, so the
  // useFrameAliveRecovery load listener is attached before vi.useFakeTimers() installs
  // the fake clock.  Without this, on a busy runner the effects can still be queued
  // in a microtask when fake timers are installed, causing reload()'s fireEvent.load
  // to fire before the listener is in place.
  await act(async () => {})
  // The test DOM gives a cross-origin frame no window, so the frame gets a stand-in
  // that records the ping and is the `source` its answer comes from.
  const pings: unknown[] = []
  const win = { postMessage: (message: unknown) => pings.push(message) }
  Object.defineProperty(frame, 'contentWindow', { configurable: true, value: win })
  vi.useFakeTimers()
  const reload = () => act(() => { fireEvent.load(frame) })
  const wait = () => act(() => { vi.advanceTimersByTime(FRAME_ALIVE_TIMEOUT_MS) })
  const answerPing = () => act(() => {
    const ev = new MessageEvent('message', { data: { type: FRAME_ALIVE_MESSAGE_TYPE } })
    Object.defineProperty(ev, 'source', { value: win })
    window.dispatchEvent(ev)
  })
  return { frame, pings, reload, wait, answerPing }
}

describe('a dashboard frame reloaded onto its spent url', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    retry.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('mints a new url when the reloaded document does not answer', async () => {
    const { pings, reload, wait } = await mount()
    reload()
    expect(pings).toContainEqual({ type: FRAME_PING_MESSAGE_TYPE })
    wait()
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('leaves a document that answers alone', async () => {
    const { reload, wait, answerPing } = await mount()
    reload()
    answerPing()
    wait()
    expect(retry).not.toHaveBeenCalled()
  })

  it('stops after a few tries in a row, and an answer restarts the count', async () => {
    const { reload, wait, answerPing } = await mount()
    for (let i = 0; i < FRAME_ALIVE_MAX_RECOVERIES + 2; i++) {
      reload()
      wait()
    }
    expect(retry).toHaveBeenCalledTimes(FRAME_ALIVE_MAX_RECOVERIES)
    answerPing()
    reload()
    wait()
    expect(retry).toHaveBeenCalledTimes(FRAME_ALIVE_MAX_RECOVERIES + 1)
  })

  it('does not wait on a frame that may not run script', async () => {
    const { reload, wait } = await mount(false)
    reload()
    wait()
    expect(retry).not.toHaveBeenCalled()
  })
})
