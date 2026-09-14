import { screen, fireEvent, waitFor, renderHook, render, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { renderWithProviders } from '../test/helpers'
import { sseSlots } from '../store/dashboardSlice'
import { store as appStore } from '../store'
import SendToInstanceSubmenu from './SendToInstanceSubmenu'
import ActionFailureNotice from './ActionFailureNotice'
import { api, ApiError } from '../api/client'
import type { InstanceView } from '../api/client'
import { __resetActionFailureForTests, useActionFailure } from '../utils/actionFailure'
import { __resetErrorJournalForTests, MAX_MESSAGE, recordError } from '../utils/errorReport'

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
  return renderWithProviders(<SendToInstanceSubmenu slotKey="zzq-slot" variant={variant} />)
}

describe('SendToInstanceSubmenu', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    listInstances.mockReset()
    sendSessionToInstance.mockReset()
    sendSessionToInstance.mockResolvedValue({ resume_mode: 'session_load' } as never)
    // The page notice is raised only while the copied session is still listed,
    // and the reporter reads the module-level store, not the Provider's. No
    // title, so the notice keeps its unnamed lead unless a test seeds one.
    appStore.dispatch(sseSlots([{ key: 'zzq-slot' } as never]))
  })

  afterEach(() => {
    appStore.dispatch(sseSlots([]))
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
      .toBe('The copy was not delivered — this session is unchanged.'))
    const peerWords = await screen.findByText('zzq-peer-refused')
    expect(peerWords).toBeInTheDocument()
    expect(screen.queryByText('Sent')).toBeNull()
  })

  it('redacts credentials from peer refusals in the page and inline row notices', async () => {
    const bearerSecret = 'zzqBearerFixture123'
    const tokenSecret = 'zzqTokenFixture123'
    const detail = `instance refused Authorization: Bearer ${bearerSecret} token=${tokenSecret} — update it, then reconnect`
    sendSessionToInstance.mockRejectedValue(new ApiError(502, detail))
    render(<MemoryRouter><ActionFailureNotice /></MemoryRouter>)
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))

    const pageNotice = await screen.findByTestId('session-action-error')
    const inlineNotice = within(screen.getByTitle('zzq-peer')).getByRole('alert')
    expect(pageNotice).toHaveTextContent('[redacted]')
    expect(inlineNotice).toHaveTextContent('[redacted]')
    expect(pageNotice).toHaveTextContent('update it, then reconnect')
    expect(inlineNotice).toHaveTextContent('update it, then reconnect')
    expect(document.body).not.toHaveTextContent(bearerSecret)
    expect(document.body).not.toHaveTextContent(tokenSecret)
  })

  it('sets the peer sentence off from the completed local sentence', async () => {
    const detail = 'instance is running an older Kiro Crew — update it, then reconnect'
    sendSessionToInstance.mockRejectedValue(new ApiError(502, detail))
    render(<MemoryRouter><ActionFailureNotice /></MemoryRouter>)
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))

    const pageNotice = await screen.findByTestId('session-action-error')
    expect(pageNotice).toHaveTextContent(`The copy was not delivered — this session is unchanged. “${detail}”`)
    expect(pageNotice.textContent).not.toContain('unchanged. instance')
  })

  it('caps a peer refusal longer than the journal budget and still resolves its report', async () => {
    // The peer's `{"error": …}` is re-emitted verbatim in the 502 and unwrapped
    // into ApiError.message, so its length is the peer's to choose. The page
    // notice has no cap of its own; the handler applies the journal's.
    __resetErrorJournalForTests()
    const detail = 'zzq-peer refused: ' + 'x'.repeat(MAX_MESSAGE * 3)
    // What the transport journals for this response before throwing it.
    const journaled = recordError({ source: 'api', message: detail, status: 502, endpoint: '/api/instances/i1/send' })
    sendSessionToInstance.mockRejectedValue(new ApiError(502, detail))
    render(<MemoryRouter><ActionFailureNotice /></MemoryRouter>)
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))

    const { result } = renderHook(() => useActionFailure())
    await waitFor(() => expect(result.current.failure).not.toBeNull())
    const failure = result.current.failure!
    expect(failure.message).toBe(
      `The copy was not delivered — this session is unchanged. “${detail.slice(0, MAX_MESSAGE)}\n[truncated]”`,
    )
    // Same object as the journal's entry: the hand-off carries status and endpoint.
    expect(failure.report).toBe(journaled)
    // The row's inline notice and its hand-off item get the same capped text.
    const inlineNotice = within(screen.getByTitle('zzq-peer')).getByRole('alert')
    expect(inlineNotice.textContent).toContain('[truncated]')
    expect(inlineNotice.textContent!.length).toBeLessThan(MAX_MESSAGE + 100)
    expect(document.body.textContent).not.toContain('x'.repeat(MAX_MESSAGE + 1))
  })

  it('passes a peer refusal within the journal budget through whole, report attached', async () => {
    __resetErrorJournalForTests()
    const detail = 'zzq-peer refused: the session is too large'
    const journaled = recordError({ source: 'api', message: detail, status: 502, endpoint: '/api/instances/i1/send' })
    sendSessionToInstance.mockRejectedValue(new ApiError(502, detail))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))

    const { result } = renderHook(() => useActionFailure())
    await waitFor(() => expect(result.current.failure).not.toBeNull())
    expect(result.current.failure!.message)
      .toBe(`The copy was not delivered — this session is unchanged. “${detail}”`)
    expect(result.current.failure!.report).toBe(journaled)
  })

  it('a transport failure puts no raw exception text in the page notice', async () => {
    // "Failed to fetch" is not a sentence anyone wrote for the reader; the store's
    // rule keeps it in the journal, and the row is the only place it shows.
    sendSessionToInstance.mockRejectedValue(new TypeError('Failed to fetch'))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    const { result } = renderHook(() => useActionFailure())
    await waitFor(() => expect(result.current.failure?.message)
      .toBe('The copy was not delivered — this session is unchanged.'))
    expect(await screen.findByText('Failed to fetch')).toBeInTheDocument()
  })


  it('the page notice heads with what failed to leave, not with a session update that never happened', async () => {
    // The shared lead reads "Couldn’t update", which is false of a copy: nothing
    // here changed. The send names its own action and the session it copied.
    sendSessionToInstance.mockRejectedValue(new Error('zzq-peer-refused'))
    // The reporter reads the session's title from the module-level store, not
    // the Provider's, so that is the one to seed.
    appStore.dispatch(sseSlots([{ key: 'zzq-slot', title: 'zzq-title' } as never]))
    try {
      mount([instance()])
      fireEvent.click(await screen.findByTitle('zzq-peer'))
      const { result } = renderHook(() => useActionFailure())
      await waitFor(() => expect(result.current.failure?.heading).toBe('Couldn’t send a copy of “zzq-title”'))
      expect(result.current.failure?.subject).toBe('zzq-title')
      expect(result.current.failure?.heading).not.toMatch(/update/i)
    } finally {
      appStore.dispatch(sseSlots([]))
    }
  })

  it('a long peer sentence wraps inside a capped notice instead of widening the flyout', async () => {
    sendSessionToInstance.mockRejectedValue(new Error('zzq-' + 'peer refused the transfer because '.repeat(3) + 'the session is too large'))
    mount([instance()])
    fireEvent.click(await screen.findByTitle('zzq-peer'))
    const alert = await screen.findByRole('alert')
    // The cap sits on the wrapper the row lays out, so the inline notice — whose
    // message span already shrinks and breaks anywhere — has a width to wrap in.
    expect(alert.parentElement?.className).toMatch(/\bmax-w-/)
    expect(alert.querySelector('span[style]')?.getAttribute('style')).toContain('overflow-wrap: anywhere')
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
      .toBe('The copy was not delivered — this session is unchanged.'))
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
