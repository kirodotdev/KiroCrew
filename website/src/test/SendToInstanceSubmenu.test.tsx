/**
 * "Send a copy to ▸ <instance>" — the cross-instance session transfer entry.
 *
 * jsdom cannot drive a real Radix submenu open (no PointerEvent), the same
 * limitation ChatSidebar.moveToFolder.test.tsx documents. So this file locks the
 * three pieces that ARE reliably testable:
 *   (1) the wrapper's self-hiding contract (no instances → no menu entry),
 *   (2) InstanceSendItems — the row list, rendered against a plain Item stub, and
 *   (3) the wrapper's mutation outcome, using lightweight menu primitives so a
 *       send can be driven without a live Radix submenu.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError, type InstanceView } from '../api/client'

const mocks = vi.hoisted(() => ({
  listInstances: vi.fn(),
  sendSessionToInstance: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  },
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

vi.mock('../components/ui/dropdown-menu', () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => <>{children}</>
  const Item = ({ title, disabled, onSelect, children }: {
    title?: string
    disabled?: boolean
    onSelect?: (event: Event) => void
    children?: React.ReactNode
  }) => (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={() => onSelect?.(new Event('select'))}
    >
      {children}
    </button>
  )
  return {
    DropdownMenu: Passthrough,
    DropdownMenuContent: Passthrough,
    DropdownMenuSub: Passthrough,
    DropdownMenuSubTrigger: Passthrough,
    DropdownMenuSubContent: Passthrough,
    DropdownMenuItem: Item,
  }
})

import SendToInstanceSubmenu, { InstanceSendItems } from '../components/SendToInstanceSubmenu'
import { DropdownMenu, DropdownMenuContent } from '../components/ui/dropdown-menu'
import { store } from '../store'
import { removeSlotOptimistic, sseSlots } from '../store/dashboardSlice'
import { __resetActionFailureForTests, useActionFailure } from '../utils/actionFailure'

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

/** A plain stand-in for the Radix menu-item primitive. */
function StubItem({ title, disabled, onSelect, children }: {
  title?: string
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      data-testid="row"
      onClick={() => onSelect?.(new Event('select'))}
    >
      {children}
    </button>
  )
}

function renderWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DropdownMenu open>
        <DropdownMenuContent forceMount>
          <SendToInstanceSubmenu slotKey="slot-1" variant="dropdown" />
        </DropdownMenuContent>
      </DropdownMenu>
    </QueryClientProvider>,
  )
}

describe('SendToInstanceSubmenu', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    mocks.listInstances.mockReset()
    mocks.sendSessionToInstance.mockReset()
    store.dispatch(sseSlots([
      { key: 'slot-1', title: 'Session one', mode: 'chat' } as never,
    ]))
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

  it('reports a send failure while the source session is still listed', async () => {
    mocks.listInstances.mockResolvedValue({
      active: true, instances: [inst({ id: 'devdesk' })], warm_set_cap: 5,
    })
    mocks.sendSessionToInstance.mockRejectedValue(new ApiError(409, 'peer refused'))
    const { result } = renderHook(() => useActionFailure())
    renderWrapper()
    const row = await screen.findByTitle('devdesk')
    act(() => { row.click() })
    await waitFor(() =>
      expect(mocks.sendSessionToInstance).toHaveBeenCalledWith('devdesk', 'slot-1'))
    await waitFor(() => expect(result.current.failure?.message).toContain('peer refused'))
    expect(result.current.failure?.subject).toBe('Session one')
  })

  it('reports nothing when the source session leaves the list before send fails', async () => {
    mocks.listInstances.mockResolvedValue({
      active: true, instances: [inst({ id: 'devdesk' })], warm_set_cap: 5,
    })
    let rejectSend!: (reason: unknown) => void
    mocks.sendSessionToInstance.mockImplementation(() =>
      new Promise((_resolve, reject) => { rejectSend = reject }))
    const { result } = renderHook(() => useActionFailure())
    renderWrapper()
    const row = await screen.findByTitle('devdesk')
    act(() => { row.click() })
    await waitFor(() =>
      expect(mocks.sendSessionToInstance).toHaveBeenCalledWith('devdesk', 'slot-1'))
    act(() => { store.dispatch(removeSlotOptimistic('slot-1')) })
    await act(async () => { rejectSend(new Error('peer refused')); await Promise.resolve() })
    expect(await screen.findByText('peer refused')).toBeTruthy()
    expect(result.current.failure).toBeNull()
  })
})

describe('InstanceSendItems', () => {
  it('enables a connected instance and calls onSend with its id', () => {
    const onSend = vi.fn()
    render(
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
    render(
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
    render(
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
    render(
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

  it('reports success on the row itself', () => {    render(
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

  it('leaves no error surface on the row, which the page notice owns', () => {
    render(
      <InstanceSendItems
        instances={[inst({ id: 'devdesk' })]}
        states={{ devdesk: { kind: 'idle' } }}
        onSend={vi.fn()}
        Item={StubItem}
      />,
    )
    expect(screen.queryByText('Failed')).toBeNull()
    expect(screen.queryByText('Sent')).toBeNull()
  })

  it('a repeat send stays available after success (copy semantics)', () => {
    const onSend = vi.fn()
    render(
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
})
