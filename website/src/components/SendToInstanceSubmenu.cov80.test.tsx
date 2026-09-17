import { screen, fireEvent, waitFor, renderHook } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseConnected } from '../store/dashboardSlice'
import SendToInstanceSubmenu from './SendToInstanceSubmenu'
import { api } from '../api/client'
import type { InstanceView } from '../api/client'
import { __resetActionFailureForTests, useActionFailure } from '../utils/actionFailure'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, listInstances: vi.fn(), sendSessionToInstance: vi.fn() },
  }
})

/**
 * happy-dom cannot drive a real Radix submenu open (no PointerEvent), which is
 * why the row list is exported separately. Stub the two menu families down to
 * plain elements so the CONTAINER — its query, its mutation callbacks and its
 * family switch — is reachable.
 */
function stubMenu(prefix: string) {
  const Item = ({ children, disabled, onSelect, title, ...rest }: {
    children?: React.ReactNode
    disabled?: boolean
    onSelect?: (e: Event) => void
    title?: string
    'aria-describedby'?: string
  }) => (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={() => onSelect?.(new Event('select', { cancelable: true }))}
      {...rest}
    >
      {children}
    </button>
  )
  const Pass = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>
  return {
    [`${prefix}Sub`]: Pass,
    [`${prefix}SubTrigger`]: Pass,
    [`${prefix}SubContent`]: Pass,
    [`${prefix}Item`]: Item,
  }
}

vi.mock('./ui/dropdown-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...stubMenu('DropdownMenu'),
}))
vi.mock('./ui/context-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...stubMenu('ContextMenu'),
}))

const listInstances = vi.mocked(api.listInstances)
const sendSessionToInstance = vi.mocked(api.sendSessionToInstance)

function instance(over: Partial<InstanceView> = {}): InstanceView {
  return {
    id: 'i1',
    name: 'zzq-peer',
    status: { state: 'connected' },
    ...over,
  } as InstanceView
}

function mount(instances: InstanceView[], variant: 'dropdown' | 'context' = 'dropdown') {
  listInstances.mockResolvedValue({ instances } as never)
  // Seeded, not inherited: createTestStore models a DISCONNECTED dashboard, which
  // the offline sink guard would refuse every send here asserts.
  const store = createTestStore()
  store.dispatch(sseConnected())
  return renderWithProviders(<SendToInstanceSubmenu slotKey="zzq-slot" variant={variant} />, { store })
}

describe('SendToInstanceSubmenu', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    listInstances.mockReset()
    sendSessionToInstance.mockReset()
    sendSessionToInstance.mockResolvedValue({ resume_mode: 'session_load' } as never)
  })

  it('renders nothing while there are no configured instances', async () => {
    const { container } = mount([])
    await waitFor(() => expect(listInstances).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('renders nothing when the instances feature is disabled (query rejects)', async () => {
    listInstances.mockRejectedValue(new Error('zzq-403'))
    const { container } = renderWithProviders(
      <SendToInstanceSubmenu slotKey="zzq-slot" variant="dropdown" />,
    )
    await waitFor(() => expect(listInstances).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('renders the trigger and one row per instance', async () => {
    mount([instance(), instance({ id: 'i2', name: 'zzq-peer-2' })])
    expect(await screen.findByText('Send a copy to')).toBeInTheDocument()
    expect(screen.getByText('zzq-peer')).toBeInTheDocument()
    expect(screen.getByText('zzq-peer-2')).toBeInTheDocument()
  })

  it('a successful send marks the row Sent and keeps the local session key', async () => {
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    await waitFor(() =>
      expect(sendSessionToInstance).toHaveBeenCalledWith('i1', 'zzq-slot'))
    expect(await screen.findByText('Sent')).toBeInTheDocument()
  })

  it('a prefix-only resume is reported as transcript-only, never plain Sent', async () => {
    sendSessionToInstance.mockResolvedValue({ resume_mode: 'prefix' } as never)
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    expect(await screen.findByText('Sent (transcript only)')).toBeInTheDocument()
  })

  it('an older peer that reports no mode stays plain Sent', async () => {
    sendSessionToInstance.mockResolvedValue({} as never)
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    expect(await screen.findByText('Sent')).toBeInTheDocument()
  })

  it('a refused transfer keeps the peer\'s own words on the row AND reports to the page notice', async () => {
    sendSessionToInstance.mockRejectedValue(new Error('zzq-peer-refused'))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    await waitFor(() => expect(sendSessionToInstance).toHaveBeenCalled())
    const { result } = renderHook(() => useActionFailure())
    await waitFor(() => expect(result.current.failure?.message)
      .toBe("Couldn't send a copy to that instance."))
    const peerWords = await screen.findByText('zzq-peer-refused')
    expect(peerWords).toBeInTheDocument()
    expect(screen.queryByText('Sent')).toBeNull()
  })

  it('the refusal is a readable alert, not a hover-only title a touch user never reaches', async () => {
    sendSessionToInstance.mockRejectedValue(new Error('zzq-peer-refused'))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('zzq-peer-refused')
    expect(screen.queryByTitle('zzq-peer-refused')).toBeNull()
  })

  it('the hand-off is a sibling menu item describing the alert, never nested inside it', async () => {
    sendSessionToInstance.mockRejectedValue(new Error('zzq-peer-refused'))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    const alert = await screen.findByRole('alert')
    const handoff = await screen.findByText('Ask the agent')
    expect(alert.contains(handoff)).toBe(false)
    const described = document.querySelector(`[aria-describedby="${alert.id}"]`)
    expect(described).not.toBeNull()
    expect(described!.contains(handoff)).toBe(true)
  })

  it('a non-Error rejection still reports rather than throwing', async () => {
    sendSessionToInstance.mockRejectedValue('zzq-not-an-error')
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    await waitFor(() => expect(sendSessionToInstance).toHaveBeenCalled())
    const { result } = renderHook(() => useActionFailure())
    await waitFor(() => expect(result.current.failure?.message)
      .toBe("Couldn't send a copy to that instance."))
    expect(screen.queryByText('Failed')).toBeNull()
  })

  it('a disconnected peer renders disabled with a hint instead of vanishing', async () => {
    mount([instance({ status: { state: 'disconnected' } as never })])
    const row = await screen.findByTitle('zzq-peer — not connected')
    expect(row).toBeDisabled()
    fireEvent.click(row)
    expect(sendSessionToInstance).not.toHaveBeenCalled()
  })

  it('works the same inside the context-menu family', async () => {
    mount([instance()], 'context')
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    await waitFor(() =>
      expect(sendSessionToInstance).toHaveBeenCalledWith('i1', 'zzq-slot'))
  })
})
