/**
 * A crew a pane announces that closes a loop back onto this dashboard. The
 * gateway's cycle guard can only run once the hop is open, so it refuses at
 * CONNECT, after the add has already stored the row. The row must not outlive
 * that refusal: a loop does not clear on a retry, so a kept row is a crew in
 * Remote Crew that can never connect.
 *
 * The mocks are this file's own: a vi.mock factory applies only to the file it
 * is written in.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { CHAINED_CREW_REFUSED_MESSAGE } from '../lib/chainAnnounce'
import { api } from '../api/client'
import { mockConnectedCd1 } from './InstancesViewportTestUtils'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'added-1' }),
    removeInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    relayViewerCrews: vi.fn().mockResolvedValue({ crews: [] }),
  },
}))

const LOOP_REASON = 'that crew is this dashboard reached through a loop.'

const parentPaneWarm = () =>
  createTestStore({
    instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
  })

async function announceFromParentPane() {
  await act(async () => {
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: 'http://127.0.0.1:7778',
        data: { type: 'mc-instance-ready', v: 1, id: 'c-2', name: 'Looped', sshHost: 'cd-2-alias', remotePort: 5476, port: 40002 },
      }),
    )
  })
}

// What the api client throws for the gateway's 502 on a failed connect: the
// ApiError message is the human reason, and the raw body keeps the status fields.
function failedConnect(error: string, errorCode?: string): Error {
  const body = JSON.stringify({
    instance_id: 'added-1',
    state: 'error',
    error,
    ...(errorCode ? { error_code: errorCode } : {}),
    code: 'instance_connect_failed',
  })
  return Object.assign(new Error(error), { status: 502, body })
}

const connectedParent = { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777, token: 'tok' }

function connectAnswers(failure: Error) {
  vi.mocked(api.connectInstance).mockImplementation(async (id: string) => {
    if (id === 'added-1') throw failure
    return connectedParent as Awaited<ReturnType<typeof api.connectInstance>>
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  mockConnectedCd1()
})

describe('a chained crew the cycle guard refuses', () => {
  it('removes the row the announcement added and tells the pane why', async () => {
    connectAnswers(failedConnect(LOOP_REASON, 'chain_cycle'))
    renderWithProviders(<InstancesViewport />, { store: parentPaneWarm() })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    const pane = document.querySelector('iframe') as HTMLIFrameElement
    const toPane = vi.fn()
    Object.defineProperty(pane, 'contentWindow', { value: { postMessage: toPane }, configurable: true })

    await announceFromParentPane()

    await waitFor(() => expect(api.removeInstance).toHaveBeenCalledWith('added-1'))
    await waitFor(() =>
      expect(toPane).toHaveBeenCalledWith(
        expect.objectContaining({ type: CHAINED_CREW_REFUSED_MESSAGE, id: 'c-2', reason: LOOP_REASON }),
        expect.stringMatching(/:7778$/),
      ),
    )
  })

  it('keeps a row whose connect failed for any other reason', async () => {
    // A transient failure (the host is down for a moment) is not a loop: the row
    // is still the crew the user connected, and a later connect can succeed.
    connectAnswers(failedConnect('ssh exited 255'))
    renderWithProviders(<InstancesViewport />, { store: parentPaneWarm() })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    await announceFromParentPane()

    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('added-1'))
    expect(api.removeInstance).not.toHaveBeenCalled()
  })
})
