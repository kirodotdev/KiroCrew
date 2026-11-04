/**
 * The viewport's presentation of a crew whose connection method this build has
 * no transport for (#13337). The gateway refuses such a connect itself (409
 * `connection_method_unsupported`, since #20668); these tests pin what the pane,
 * its Retry and the model relayed to embedded panes say about that crew.
 *
 * Its own file rather than more rows in `InstancesViewport.test.tsx`, which is
 * already on the file-line-cap baseline and must not grow.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { friendlyErrText } from '../api/apiError'
import type { InstanceView } from '../api/client'
import { __resetErrorJournalForTests, __resetNavSeamForTests } from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn(),
    connectInstance: vi.fn(),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'added-1' }),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
  },
}))
import { api } from '../api/client'

const edge = (over: Partial<InstanceView> = {}): InstanceView => ({
  id: 'edge',
  name: 'Edge Crew',
  ssh_host: 'leaked-host',
  remote_port: 7777,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  connection_method: 'outbound',
  was_connected: true,
  status: { instance_id: 'edge', state: 'disconnected', remote_port: 7777 },
  ...over,
})

const cloudOne: InstanceView = {
  id: 'cd-1',
  name: 'Cloud One',
  ssh_host: 'cd-1-alias',
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
  vi.mocked(api.connectInstance).mockResolvedValue({ state: 'connected', local_port: 7777, token: 'tok' })
  __resetInstanceFailuresForTests()
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
})

function renderActiveEdge() {
  vi.mocked(api.listInstances).mockResolvedValue({ instances: [edge()], warm_set_cap: 5 })
  const store = createTestStore({
    instances: { warm: {}, activeId: 'edge', mru: ['edge'], unread: {} },
  })
  return renderWithProviders(<InstancesViewport />, { store })
}

describe('InstancesViewport, unmapped connection method', () => {
  it('shows the ordinary disconnected pane, with no hint of its own', async () => {
    // First Principles items 6-7 on #13337: the pane adds nothing for an
    // unmapped method. Its own status word and primary Retry stay as for any
    // disconnected crew, and the gateway's refusal is what Retry then shows.
    renderActiveEdge()

    // The list has loaded once the pane names the crew rather than its id.
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(await screen.findAllByText('Edge Crew')).not.toHaveLength(0)
    expect(screen.getByText('Disconnected')).toBeInTheDocument()
    expect(screen.queryByText(/doesn't support the outbound connection method/)).toBeNull()
    expect(screen.getByRole('button', { name: /Retry/ }).className).toMatch(/bg-accent/)
  })

  it("shows the gateway's own connection_method_unsupported answer to Retry as an error", async () => {
    // Retry must visibly do something (UX Watch on #13337), and a gateway
    // refusal is an error like any other (errors-use-error-notice), so it renders
    // through the panel's ErrorNotice. The body is the gateway's 409 as
    // handlers_instances.py sends it for UnsupportedConnectionMethodError, worded
    // by `_unsupported_method_message`, and the message goes through the client's
    // real unwrapping, so this pins what the user reads.
    const gatewayText =
      "crew 'edge' uses connection method 'outbound', which this version of Kiro Crew cannot connect over (supported: ssh, ssm, fargate)"
    const body = JSON.stringify({
      instance_id: 'edge', state: 'disconnected', error: gatewayText, code: 'connection_method_unsupported',
    })
    vi.mocked(api.connectInstance).mockRejectedValue(
      Object.assign(new Error(friendlyErrText(409, body)), { status: 409, body }),
    )
    renderActiveEdge()

    fireEvent.click(await screen.findByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalled())
    await act(async () => {})
    expect(await screen.findByTestId('instances-viewport-panel-error')).toHaveTextContent(gatewayText)
    // UX Watch on #13337: the refusal is said once, by the notice alone.
    expect(screen.getAllByText(/cannot connect over|doesn't support/)).toHaveLength(1)
  })

  it('shows any other Retry failure on an unsupported crew as an error too', async () => {
    // A gateway that answers with something else (here a 502 with no code).
    const body = JSON.stringify({ error: 'gateway restarting' })
    vi.mocked(api.connectInstance).mockRejectedValue(
      Object.assign(new Error(friendlyErrText(502, body)), { status: 502, body }),
    )
    renderActiveEdge()

    fireEvent.click(await screen.findByRole('button', { name: /Retry/ }))
    expect(await screen.findByTestId('instances-viewport-panel-error')).toHaveTextContent('gateway restarting')
  })

  it('relays the crew to embedded panes with its method as the detail', async () => {
    // Opus advisory on #13337: a pane's own switcher showed a blank detail for
    // the crew. The relayed model gains no field: only the detail changes.
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [cloudOne, edge({ ssh_host: '' })],
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 'tok' } },
        activeId: 'cd-1', mru: ['cd-1', 'edge'], unread: {}, ready: { 'cd-1': true },
      },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const frame = () =>
      [...document.querySelectorAll('iframe')].find(f => f.src.includes(':7778')) as HTMLIFrameElement
    await waitFor(() => expect(frame()).toBeDefined())
    const w = { postMessage: vi.fn() } as unknown as Window
    Object.defineProperty(frame(), 'contentWindow', { configurable: true, get: () => w })
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-embedded-ready', v: 1 }, origin: 'http://127.0.0.1:7778', source: w,
      }))
    })

    const lastModel = () =>
      vi.mocked(w.postMessage).mock.calls.at(-1)?.[0] as { tabs: Array<Record<string, unknown>> } | undefined
    await waitFor(() => expect(lastModel()?.tabs.map(t => t.id)).toContain('edge'))
    const tabs = (lastModel() as { tabs: Array<Record<string, unknown>> }).tabs
    expect(tabs.find(t => t.id === 'edge')).toMatchObject({ sshHost: 'outbound' })
    expect(tabs.find(t => t.id === 'edge')).not.toHaveProperty('unsupported')
  })
})
