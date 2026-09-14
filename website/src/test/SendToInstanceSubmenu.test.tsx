/**
 * "Send a copy to ▸ <instance>" — the cross-instance session transfer entry.
 *
 * jsdom cannot drive a real Radix submenu open (no PointerEvent), the same
 * limitation ChatSidebar.moveToFolder.test.tsx documents. So this file locks the
 * two halves that ARE reliably testable:
 *   (1) the wrapper's self-hiding contract (no instances → no menu entry), and
 *   (2) InstanceSendItems — the row list, rendered against a plain Item stub, so
 *       the disabled / outcome logic is asserted without a live submenu.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { InstanceView } from '../api/client'
import { createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'

const mocks = vi.hoisted(() => ({
  listInstances: vi.fn(),
  sendSessionToInstance: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import SendToInstanceSubmenu, { InstanceSendItems } from '../components/SendToInstanceSubmenu'
import { DropdownMenu, DropdownMenuContent } from '../components/ui/dropdown-menu'

/** Minimal InstanceView; only id/name/status are read by the component. */
function inst(over: Partial<InstanceView> & { id: string }): InstanceView {
  return {
    name: over.id,
    ssh_host: 'host',
    remote_port: 7777,
    local_port: 7778,
    ttl: '20h',
    remote_bin: '',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: 'ec2-user',
    was_connected: true,
    status: { instance_id: over.id, state: 'connected' },
    ...over,
  } as InstanceView
}

/** Every select event the rows saw, so a test can assert one was defaulted. */
const selects: Event[] = []

/** A plain stand-in for the Radix menu-item primitive. */
function StubItem({ title, disabled, onSelect, children, 'aria-describedby': describedBy }: {
  title?: string
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: React.ReactNode
  'aria-describedby'?: string
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      aria-describedby={describedBy}
      data-testid="row"
      onClick={() => {
        // Cancelable, because Radix keys menu dismissal on defaultPrevented and
        // a non-cancelable event would make a refusing handler unobservable.
        const event = new Event('select', { cancelable: true })
        selects.push(event)
        onSelect?.(event)
      }}
    >
      {children}
    </button>
  )
}

/** InstanceSendItems reads the gateway flag from the store itself. */
function withStore(ui: React.ReactElement, connected = true) {
  const store = createTestStore()
  if (connected) store.dispatch(sseConnected())
  return render(<Provider store={store}>{ui}</Provider>)
}

function renderWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore()
  store.dispatch(sseConnected())
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <DropdownMenu open>
          <DropdownMenuContent forceMount>
            <SendToInstanceSubmenu slotKey="slot-1" variant="dropdown" />
          </DropdownMenuContent>
        </DropdownMenu>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('SendToInstanceSubmenu', () => {
  beforeEach(() => {
    mocks.listInstances.mockReset()
    mocks.sendSessionToInstance.mockReset()
  })

  it('renders no menu entry when no instances are configured', async () => {
    mocks.listInstances.mockResolvedValue({ active: true, instances: [], warm_set_cap: 5 })
    renderWrapper()
    // Nothing to send to → the entry must not exist at all, rather than showing
    // a dead "no targets" row.
    expect(screen.queryByText('Send a copy to')).toBeNull()
  })

  it('renders no menu entry when the feature is disabled (listInstances rejects 403)', async () => {
    mocks.listInstances.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    renderWrapper()
    expect(screen.queryByText('Send a copy to')).toBeNull()
  })

  it('mounts the trigger once at least one instance exists', async () => {
    mocks.listInstances.mockResolvedValue({
      active: true, instances: [inst({ id: 'devdesk' })], warm_set_cap: 5,
    })
    renderWrapper()
    expect(await screen.findByText('Send a copy to')).toBeTruthy()
  })
})

describe('InstanceSendItems', () => {
  it('enables a connected instance and calls onSend with its id', () => {
    const onSend = vi.fn()
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{}}
        onSend={onSend}
        Item={StubItem}
      />,
    )
    const row = screen.getByTestId('row')
    expect(row).not.toBeDisabled()
    row.click()
    expect(onSend).toHaveBeenCalledWith('devdesk')
  })

  it('disables a disconnected instance and shows the hint instead of hiding it', () => {
    const onSend = vi.fn()
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'offline', status: { instance_id: 'offline', state: 'disconnected' } })]}
        states={{}}
        onSend={onSend}
        Item={StubItem}
      />,
    )
    const row = screen.getByTestId('row')
    // Still listed — a peer the user configured must not silently vanish.
    expect(row).toBeDisabled()
    expect(screen.getByText('not connected')).toBeTruthy()
    row.click()
    expect(onSend).not.toHaveBeenCalled()
  })

  it('disables the row while a send is in flight', () => {
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{ devdesk: { kind: 'sending' } }}
        onSend={vi.fn()}
        Item={StubItem}
      />,
    )
    expect(screen.getByTestId('row')).toBeDisabled()
  })

  it('distinguishes a transcript-only copy from a full one', () => {
    // The whole point of the feature is that context survives the hop. A copy
    // that degraded to the transcript must NOT read as a plain "Sent", or the
    // user walks to the other machine and discovers the loss mid-task.
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{ devdesk: { kind: 'sent', transcriptOnly: true } }}
        onSend={vi.fn()}
        Item={StubItem}
      />,
    )
    expect(screen.getByText('Sent (transcript only)')).toBeTruthy()
    expect(screen.queryByText('Sent')).toBeNull()
  })

  it('reports success on the row itself', () => {    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{ devdesk: { kind: 'sent' } }}
        onSend={vi.fn()}
        Item={StubItem}
      />,
    )
    // The transfer's only effect is on another machine, so the row is the one
    // place a completed copy can be distinguished from a dropped one.
    expect(screen.getByText('Sent')).toBeTruthy()
  })

  it("surfaces the peer's own message through ErrorNotice, with the hand-off as a sibling item", () => {
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{ devdesk: { kind: 'error', message: 'peer refused the transfer' } }}
        onSend={vi.fn()}
        Item={StubItem}
      />,
    )
    const notice = screen.getByRole('alert')
    expect(notice.textContent).toContain('peer refused the transfer')
    const handoff = screen.getByText('Ask the agent').closest('button')!
    expect(handoff.getAttribute('aria-describedby')).toBe(notice.id)
    expect(notice.id).toBeTruthy()
  })

  it('a repeat send stays available after success (copy semantics)', () => {
    const onSend = vi.fn()
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{ devdesk: { kind: 'sent' } }}
        onSend={onSend}
        Item={StubItem}
      />,
    )
    // Sending twice is harmless: the peer allocates a new key each time.
    const row = screen.getByTestId('row')
    expect(row).not.toBeDisabled()
    row.click()
    expect(onSend).toHaveBeenCalledWith('devdesk')
  })

  it('refuses a cached-connected peer in place offline, rather than letting Radix dismiss the menu', () => {
    selects.length = 0
    const onSend = vi.fn()
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{}}
        onSend={onSend}
        Item={StubItem}
      />,
      false,
    )
    const row = screen.getByTestId('row')
    expect(row).not.toBeDisabled()
    row.click()
    expect(onSend).not.toHaveBeenCalled()
    expect(selects.at(-1)!.defaultPrevented).toBe(true)
  })

  it('defaults the select when connected too, keeping the menu open to report — the control', () => {
    selects.length = 0
    withStore(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{}}
        onSend={vi.fn()}
        Item={StubItem}
      />,
    )
    screen.getByTestId('row').click()
    expect(selects.at(-1)!.defaultPrevented).toBe(true)
  })
})
