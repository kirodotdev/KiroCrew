import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/**
 * A GAP RECOVERY IS A ROUND TRIP, and the frame under it can be replaced while it is
 * open: the ordinary dashboard read remounts the document when the package changes.
 *
 * Posting a late answer into the document that replaced the asker hands it values
 * computed for a page it is not -- and the reply looks current to that bootstrap, which
 * sees only that the generation differs and accepts it. So the asking window and the
 * package it asked for are captured when the request goes out and compared on delivery.
 */

const memberDashboard = vi.fn()
const memberDashboardRefetch = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberDashboard: (...a: unknown[]) => memberDashboard(...a),
      memberDashboardRefetch: (...a: unknown[]) => memberDashboardRefetch(...a),
    },
  }
})

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

// The mint state machine owns its own tests, and it is a round trip this one does not
// need: a stable document url is all the frame wants here.
vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? 'https://localhost/sandbox/doc' : null,
    failed: false,
    pending: false,
    stalled: false,
    retry: () => {},
  }),
}))

import CrewDynamicDashboard from './CrewDynamicDashboard'

/* The two wire types, spelled here the way the component spells them: they are module
   constants rather than exports, and `test_dashboard_frame_subscribe.py` already pins
   the component's own copy against `dashboard_frame`'s. */
const DATA_MESSAGE_TYPE = 'kirocrew-dashboard:data'
const REFETCH_MESSAGE_TYPE = 'kirocrew-dashboard:refetch'

/** The page asking for a whole read, as a message from THIS frame's window.
 *
 *  `source` is assigned after construction: jsdom validates the option against a real
 *  Window and silently drops a stand-in, and the handler's first test is that the
 *  sender is the frame it minted. */
/** Let every already-queued microtask run. A request settles through `then` ->
 *  `catch` -> `finally`, so one await reaches only the first link of the chain and the
 *  rest land after the test's barrier. */
const drain = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve()
}

const askForAWholeRead = (frame: HTMLIFrameElement) => {
  const event = new MessageEvent('message', { data: { type: REFETCH_MESSAGE_TYPE, held: 1 } })
  Object.defineProperty(event, 'source', { value: frame.contentWindow })
  window.dispatchEvent(event)
}

/** The read the route answers with. `html` is the crewmate's stored copy and
 *  `rendered_html` is what the gateway composed: the promote effect wants both, and
 *  only the composed one is ever mounted. */
const answer = (dashboard: string) => ({
  slug: 'atlas',
  member: 'Atlas',
  dashboard,
  state: 'ok',
  html: '<p>stored</p>',
  rendered_html: '<p>page</p>',
  manifest: { fields: {} },
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  read: { fields: { boards: 1 }, version: 1, generation: 'gen-a' },
})

describe('a late refetch reply', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    memberDashboardRefetch.mockReset()
  })

  it('is not posted into the document that replaced the one that asked', async () => {
    memberDashboard.mockResolvedValue(answer('page-one'))
    // The answer never settles until the test releases it, which is the window the
    // document is replaced in.
    let release: (value: unknown) => void = () => {}
    memberDashboardRefetch.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }),
    )
    const { container } = renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'atlas', member: 'Atlas' }} displayName="Atlas" />,
    )
    const frame = await waitFor(() => {
      const found = container.querySelector('iframe')
      if (!found) throw new Error('the frame never mounted')
      return found as HTMLIFrameElement
    })
    const posted: unknown[] = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: unknown) => posted.push(message) },
    })

    // The page cannot place a patch and asks for the whole read. In `act`, because the
    // ask sets the in-flight flag synchronously.
    await act(async () => {
      askForAWholeRead(frame)
      await drain()
    })
    await waitFor(() => expect(memberDashboardRefetch).toHaveBeenCalledTimes(1))

    // THE REPLACEMENT: a different window is under the frame by the time the answer
    // arrives, which is what a remount leaves behind.
    const replacement: unknown[] = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: unknown) => replacement.push(message) },
    })
    await act(async () => {
      release({ read: { fields: { boards: 99 }, version: 7, generation: 'gen-a' } })
      await drain()
    })

    expect(replacement).toEqual([])
    expect(posted).toEqual([])
  })

  it('is posted when the same document is still there', async () => {
    memberDashboard.mockResolvedValue(answer('page-one'))
    memberDashboardRefetch.mockResolvedValue({
      read: { fields: { boards: 7 }, version: 7, generation: 'gen-a' },
    })
    const { container } = renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'atlas', member: 'Atlas' }} displayName="Atlas" />,
    )
    const frame = await waitFor(() => {
      const found = container.querySelector('iframe')
      if (!found) throw new Error('the frame never mounted')
      return found as HTMLIFrameElement
    })
    const posted: Array<{ type?: string }> = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: { type?: string }) => posted.push(message) },
    })
    await act(async () => {
      askForAWholeRead(frame)
      await drain()
    })
    await waitFor(() => expect(posted.map((m) => m.type)).toEqual([DATA_MESSAGE_TYPE]))
  })
})

/**
 * THE BAND'S RETRY REDOES WHAT FAILED.
 *
 * The band is raised by one thing only: the whole-read request the page asked for did
 * not arrive. So its button has to repeat THAT request. Repeating the record read
 * instead -- `refetch()` on the dashboard query -- answers a question nobody asked: the
 * page on screen is the right page, and only its numbers are behind. And clearing the
 * band at click time states the values are current again before anything has been
 * re-read, which is the one claim the band exists to stop.
 */
describe('the retry beside the stale-values band', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    memberDashboardRefetch.mockReset()
  })

  /** Render, let the frame mount, stub its window, raise the band with a failed ask. */
  const bandUp = async () => {
    memberDashboard.mockResolvedValue(answer('page-one'))
    memberDashboardRefetch.mockRejectedValue(new Error('the fold could not be re-read'))
    const { container, getByTestId, queryByTestId } = renderWithProviders(
      <CrewDynamicDashboard target={{ kind: 'member', slug: 'atlas', member: 'Atlas' }} displayName="Atlas" />,
    )
    const frame = await waitFor(() => {
      const found = container.querySelector('iframe')
      if (!found) throw new Error('the frame never mounted')
      return found as HTMLIFrameElement
    })
    const posted: Array<{ type?: string }> = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: { type?: string }) => posted.push(message) },
    })
    // In `act`, and drained: the ask sets the in-flight flag synchronously, and the
    // request it starts settles through a chain of microtasks (`then`, `catch`,
    // `finally`), each of which carries its own state update.
    await act(async () => {
      askForAWholeRead(frame)
      await drain()
    })
    await waitFor(() => getByTestId('crew-dashboard-refetch-error-band'))
    // The press is awaited to its own barrier here, not only in each test: the
    // pending flag it sets is the state update that would otherwise land after the
    // test's last assertion.
    const press = async () => {
      const calls = memberDashboardRefetch.mock.calls.length
      // Same reason as the ask above: the press sets the in-flight flag before the
      // request it starts can answer.
      await act(async () => {
        fireEvent.click(getByTestId('crew-dashboard-refetch-retry'))
        await drain()
      })
      await waitFor(() => expect(memberDashboardRefetch.mock.calls.length).toBe(calls + 1))
    }
    /** The press has settled: the button is pressable again, which is the last state
     *  update the request makes. Used where the band stays up, so the button is there
     *  to read; where it clears, the band going is the barrier instead. */
    const settled = async () =>
      waitFor(() => expect(getByTestId('crew-dashboard-refetch-retry')).not.toBeDisabled())
    return { frame, posted, press, settled, getByTestId, queryByTestId }
  }

  it('asks for the whole read again rather than re-reading the record', async () => {
    const { press, settled } = await bandUp()
    expect(memberDashboardRefetch).toHaveBeenCalledTimes(1)
    const readsBefore = memberDashboard.mock.calls.length

    await press()
    await settled()

    expect(memberDashboardRefetch).toHaveBeenCalledTimes(2)
    expect(memberDashboardRefetch).toHaveBeenLastCalledWith('atlas', 'Atlas', 'page-one')
    // The record read is not what failed, so it is not what is repeated.
    expect(memberDashboard.mock.calls.length).toBe(readsBefore)
  })

  it('keeps the band up while the retried read fails again', async () => {
    const { press, settled, queryByTestId } = await bandUp()

    await press()
    await settled()

    // Still behind, so the notice still says so. A band that cleared here would have
    // told the reader the numbers caught up because they pressed a button.
    expect(queryByTestId('crew-dashboard-refetch-error-band')).not.toBeNull()
  })

  it('clears the band and fills the page when the retried read arrives', async () => {
    const { posted, press, queryByTestId } = await bandUp()
    memberDashboardRefetch.mockResolvedValue({
      read: { fields: { boards: 7 }, version: 7, generation: 'gen-a' },
    })

    await press()

    // The values reach the page that was behind, and only then does the notice go.
    await waitFor(() => expect(posted.map((m) => m.type)).toEqual([DATA_MESSAGE_TYPE]))
    await waitFor(() => expect(queryByTestId('crew-dashboard-refetch-error-band')).toBeNull())
  })
})
