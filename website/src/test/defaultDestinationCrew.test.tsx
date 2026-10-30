/**
 * Default destination crew: the crew "Send a copy to" lists first.
 *
 * Covers the resolver, the marked row in the send menu, the folder override in
 * Folder settings and the global picker in Settings. jsdom cannot open a real
 * Radix submenu, so the menu rows are rendered through InstanceSendItems with a
 * plain Item stub, as SendToInstanceSubmenu.test.tsx does.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import type { InstanceView } from '../api/client'

const mocks = vi.hoisted(() => ({
  patchConfig: vi.fn(),
  listInstances: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue({ dirs: [] })),
  }),
}))

import { InstanceSendItems } from '../components/SendToInstanceSubmenu'
import FolderConfigModal from '../components/FolderConfigModal'
import DefaultDestinationCrewPicker from '../components/DefaultDestinationCrewPicker'
import { renderFolderModal } from '../pages/chat-sidebar/folderModal'
import type { SidebarScope } from '../pages/chat-sidebar/scope'
import { ApiError } from '../api/apiError'
import { resolveDefaultDestination, withDefaultFirst } from '../utils/defaultDestinationCrew'
import type { ChatFolder } from '../types'

function inst(id: string, state = 'connected'): InstanceView {
  return {
    id, name: `Crew ${id}`, ssh_host: 'h', remote_port: 7777, local_port: 7778, ttl: '20h',
    remote_bin: '', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '',
    ssm_run_as: 'ec2-user', was_connected: true, status: { instance_id: id, state },
  } as InstanceView
}

function StubItem({ title, disabled, children }: {
  title?: string; disabled?: boolean; onSelect?: (event: Event) => void; children?: React.ReactNode
}) {
  return <button type="button" title={title} disabled={disabled}>{children}</button>
}

describe('resolveDefaultDestination', () => {
  const ids = ['a', 'b', 'c']
  it('is empty when neither value names a registered crew', () => {
    expect(resolveDefaultDestination(ids, '', '')).toBe('')
    expect(resolveDefaultDestination(ids, 'gone', 'also-gone')).toBe('')
  })
  it('uses the global default', () => {
    expect(resolveDefaultDestination(ids, 'b', undefined)).toBe('b')
  })
  it('lets the folder override the global default', () => {
    expect(resolveDefaultDestination(ids, 'b', 'c')).toBe('c')
  })
  it('falls back to the global default when the folder names a removed crew', () => {
    expect(resolveDefaultDestination(ids, 'b', 'gone')).toBe('b')
  })
})

describe('withDefaultFirst', () => {
  const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]
  it('moves the default to the front and keeps the rest in order', () => {
    expect(withDefaultFirst(items, 'c').map(i => i.id)).toEqual(['c', 'a', 'b'])
  })
  it('leaves the order alone without a default', () => {
    expect(withDefaultFirst(items, '').map(i => i.id)).toEqual(['a', 'b', 'c'])
    expect(withDefaultFirst(items, 'gone').map(i => i.id)).toEqual(['a', 'b', 'c'])
  })
})

describe('InstanceSendItems with a default destination', () => {
  it('marks only the default row', () => {
    render(<InstanceSendItems instances={[inst('a'), inst('b')]} states={{}} onSend={vi.fn()} Item={StubItem} defaultId="b" />)
    const marks = screen.getAllByTestId('send-to-instance-default')
    expect(marks).toHaveLength(1)
    expect(screen.getByTitle('Crew b (default)')).toContainElement(marks[0])
  })
  it('disables an offline default and keeps the other crews usable', () => {
    render(<InstanceSendItems instances={[inst('a', 'disconnected'), inst('b')]} states={{}} onSend={vi.fn()} Item={StubItem} defaultId="a" />)
    expect(screen.getByTitle(/^Crew a \(default\) — /)).toBeDisabled()
    expect(screen.getByTitle('Crew b')).not.toBeDisabled()
  })
  it('marks nothing without a default', () => {
    render(<InstanceSendItems instances={[inst('a')]} states={{}} onSend={vi.fn()} Item={StubItem} />)
    expect(screen.queryByTestId('send-to-instance-default')).toBeNull()
  })
})

describe('Folder settings: Default destination crew', () => {
  beforeEach(() => vi.clearAllMocks())
  const CREWS = [{ id: 'a', name: 'Crew a' }, { id: 'b', name: 'Crew b' }]

  function openModal(props: Partial<React.ComponentProps<typeof FolderConfigModal>> = {}) {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FolderConfigModal open={true} mode="create" parentId="" folders={[]} installedAgents={[]}
        onClose={vi.fn()} onSubmit={onSubmit} onRetryTags={vi.fn()} {...props} />,
    )
    return onSubmit
  }
  const trigger = () => screen.queryByRole('button', { name: 'Default destination crew' })

  it('is hidden with no registered crew', () => {
    openModal()
    expect(trigger()).toBeNull()
  })

  it('sends the chosen crew as a touched field', async () => {
    const onSubmit = openModal({ destinationCrews: CREWS })
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    await new Promise(resolve => requestAnimationFrame(resolve))
    fireEvent.click(trigger()!)
    fireEvent.click(await screen.findByRole('option', { name: 'Crew b' }))
    await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    const draft = onSubmit.mock.calls[0][0]
    expect(draft.defaultDestinationCrew).toBe('b')
    expect(draft.touched).toContain('defaultDestinationCrew')
  })

  it('keeps a stored crew that is no longer registered when the field is not touched', async () => {
    const folder = { id: 'f1', name: 'Ops', order: 0, default_destination_crew: 'gone' } as ChatFolder
    const onSubmit = openModal({ mode: 'edit', folder, folders: [folder], destinationCrews: CREWS })
    expect(trigger()).toHaveTextContent('gone (not registered)')
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Ops 2' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    const draft = onSubmit.mock.calls[0][0]
    expect(draft.touched).not.toContain('defaultDestinationCrew')
    expect(draft.defaultDestinationCrew).toBe('gone')
  })

  it('names the crew the global default resolves to', () => {
    openModal({ destinationCrews: CREWS, globalDestinationCrew: 'b' })
    expect(trigger()).toHaveTextContent('Use the global default (Crew b)')
  })

  it('says (none) when the global default is unset or names a removed crew', () => {
    openModal({ destinationCrews: CREWS, globalDestinationCrew: 'gone' })
    expect(trigger()).toHaveTextContent('Use the global default (none)')
  })

  it('says the crew list did not load, with Retry, instead of hiding the field', () => {
    const onRetry = vi.fn()
    openModal({ destinationCrewsFailed: true, onRetryDestinationCrews: onRetry })
    expect(screen.getByTestId('folder-config-destination-error')).toHaveTextContent("Crews couldn't load.")
    fireEvent.click(screen.getByTestId('folder-config-destination-retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('keeps the cached picker and still says a refresh failed', () => {
    openModal({ destinationCrews: CREWS, destinationCrewsFailed: true, onRetryDestinationCrews: vi.fn() })
    expect(trigger()).not.toBeNull()
    expect(screen.getByTestId('folder-config-destination-error')).toHaveTextContent("Crews couldn't load.")
    expect(screen.getByTestId('folder-config-destination-retry')).toBeInTheDocument()
  })
})

describe('Folder settings from the sidebar: the crew-list query', () => {
  beforeEach(() => vi.clearAllMocks())
  function renderFromSidebar(seed?: unknown) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // updatedAt 0 makes the seeded list stale, so mounting refetches it.
    if (seed) qc.setQueryData(['instances'], seed, { updatedAt: 0 })
    const shell = {
      createFolderMutation: { mutateAsync: vi.fn() }, updateFolderMutation: { mutateAsync: vi.fn() },
      defaultAgent: '', flatView: false, folders: [], installedAgents: [], refetchTags: vi.fn(),
      setFlatView: vi.fn(), setFolderModal: vi.fn(), tagsData: [], tagsQueryFailed: false,
    } as unknown as SidebarScope
    render(
      <QueryClientProvider client={qc}>
        {renderFolderModal(shell, { mode: 'create', parentId: '' })}
      </QueryClientProvider>,
    )
    return qc
  }

  it('surfaces a failed load and retries it in place', async () => {
    mocks.listInstances.mockRejectedValueOnce(new ApiError(500, 'boom', '{"error":"boom"}'))
    renderFromSidebar()
    expect(await screen.findByTestId('folder-config-destination-error')).toBeInTheDocument()
    mocks.listInstances.mockResolvedValueOnce({ instances: [inst('a')] })
    fireEvent.click(screen.getByTestId('folder-config-destination-retry'))
    expect(await screen.findByRole('button', { name: 'Default destination crew' })).toBeInTheDocument()
    expect(screen.queryByTestId('folder-config-destination-error')).toBeNull()
  })

  it('says a refresh failed over a cached crew list (a gateway restart)', async () => {
    mocks.listInstances.mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', ''))
    renderFromSidebar({ instances: [inst('a')] })
    expect(await screen.findByTestId('folder-config-destination-error')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Default destination crew' })).toBeInTheDocument()
  })

  it('passes the global default from the same crew-list response', async () => {
    mocks.listInstances.mockResolvedValueOnce({ instances: [inst('a')], default_destination: 'a' })
    renderFromSidebar()
    expect(await screen.findByRole('button', { name: 'Default destination crew' }))
      .toHaveTextContent('Use the global default (Crew a)')
  })

  it('stays hidden, with no error, when Instances is off', async () => {
    mocks.listInstances.mockRejectedValue(new ApiError(403, 'Instances feature is disabled',
      JSON.stringify({ error: 'Instances feature is disabled', code: 'instances_disabled' })))
    const qc = renderFromSidebar()
    await waitFor(() => expect(qc.getQueryState(['instances'])?.status).toBe('error'))
    expect(screen.queryByTestId('folder-config-destination-error')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Default destination crew' })).toBeNull()
  })
})

describe('Settings: global Default destination crew', () => {
  beforeEach(() => vi.clearAllMocks())
  function renderPicker(instances: InstanceView[], value: string) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><DefaultDestinationCrewPicker instances={instances} value={value} /></QueryClientProvider>)
  }

  it('is hidden with no registered crew', () => {
    renderPicker([], '')
    expect(screen.queryByTestId('default-destination-crew')).toBeNull()
  })

  it('calls the empty choice "No default"', () => {
    renderPicker([inst('a')], '')
    expect(screen.getByRole('button', { name: 'Default destination crew' })).toHaveTextContent('No default')
  })

  it('saves the chosen crew to instances.default_destination', async () => {
    mocks.patchConfig.mockResolvedValue({ ok: true })
    renderPicker([inst('a'), inst('b')], '')
    fireEvent.click(screen.getByRole('button', { name: 'Default destination crew' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Crew a' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.default_destination', 'a'))
  })

  it('stays disabled from the save until the refreshed crew list is back', async () => {
    let resolveSave: (v: unknown) => void = () => {}
    mocks.patchConfig.mockReturnValue(new Promise(r => { resolveSave = r }))
    let resolveRefetch: (v: unknown) => void = () => {}
    mocks.listInstances.mockReturnValue(new Promise(r => { resolveRefetch = r }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // An active ['instances'] query, so the save's invalidation has a refetch to wait on.
    qc.setQueryData(['instances'], { instances: [inst('a'), inst('b')] })
    function Observed() {
      useQuery({ queryKey: ['instances'], queryFn: () => mocks.listInstances(), staleTime: Infinity })
      return <DefaultDestinationCrewPicker instances={[inst('a'), inst('b')]} value="a" />
    }
    render(<QueryClientProvider client={qc}><Observed /></QueryClientProvider>)
    const trigger = screen.getByRole('button', { name: 'Default destination crew' })
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Crew b' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.default_destination', 'b'))
    expect(trigger).toBeDisabled()
    resolveSave({ ok: true })
    // Saved, but the crew list has not come back yet: still no second pick.
    await waitFor(() => expect(mocks.listInstances).toHaveBeenCalled())
    expect(trigger).toBeDisabled()
    resolveRefetch({ instances: [inst('a'), inst('b')], default_destination: 'b' })
    await waitFor(() => expect(trigger).not.toBeDisabled())
  })

  it('shows why a save failed', async () => {
    mocks.patchConfig.mockRejectedValue(new Error('config is read-only'))
    renderPicker([inst('a')], '')
    fireEvent.click(screen.getByRole('button', { name: 'Default destination crew' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Crew a' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('config is read-only')
    // The catalog sentence leads; the raw reason follows it, inside ErrorNotice.
    const notice = screen.getByTestId('default-destination-crew-error')
    expect(notice).toHaveTextContent(/^Couldn't save the default destination crew\./)
  })

  it('falls back to the catalog sentence when the failure has no text', async () => {
    mocks.patchConfig.mockRejectedValue(new Error(''))
    renderPicker([inst('a')], '')
    fireEvent.click(screen.getByRole('button', { name: 'Default destination crew' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Crew a' }))
    expect(await screen.findByTestId('default-destination-crew-error'))
      .toHaveTextContent("Couldn't save the default destination crew.")
  })
})
