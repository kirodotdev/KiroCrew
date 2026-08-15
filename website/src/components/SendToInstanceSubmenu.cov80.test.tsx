import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import SendToInstanceSubmenu from './SendToInstanceSubmenu'
import { api } from '../api/client'
import type { InstanceView } from '../api/client'

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
  const Item = ({ children, disabled, onSelect, title }: {
    children?: React.ReactNode
    disabled?: boolean
    onSelect?: (e: Event) => void
    title?: string
  }) => (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={() => onSelect?.(new Event('select', { cancelable: true }))}
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
  return renderWithProviders(<SendToInstanceSubmenu slotKey="zzq-slot" variant={variant} />)
}

describe('SendToInstanceSubmenu', () => {
  beforeEach(() => {
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

  it("a refused transfer surfaces the peer's own message", async () => {
    sendSessionToInstance.mockRejectedValue(new Error('zzq-peer-refused'))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    const failed = await screen.findByText('Failed')
    expect(failed.closest('[title]')!.getAttribute('title')).toBe('zzq-peer-refused')
  })

  it('a non-Error rejection falls back to the generic reason', async () => {
    sendSessionToInstance.mockRejectedValue('zzq-not-an-error')
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    const failed = await screen.findByText('Failed')
    expect(failed.closest('[title]')!.getAttribute('title')).toBe('Unknown error')
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
