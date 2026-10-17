/**
 * Default destination crew (#21547): the crew "Send a copy to" lists first.
 *
 * Covers the resolver, the marked row in the send menu, the folder override in
 * Folder settings and the global picker in Settings. jsdom cannot open a real
 * Radix submenu, so the menu rows are rendered through InstanceSendItems with a
 * plain Item stub, as SendToInstanceSubmenu.test.tsx does.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InstanceView } from '../api/client'

const mocks = vi.hoisted(() => ({
  patchConfig: vi.fn(),
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

  it('saves the chosen crew to instances.default_destination', async () => {
    mocks.patchConfig.mockResolvedValue({ ok: true })
    renderPicker([inst('a'), inst('b')], '')
    fireEvent.click(screen.getByRole('button', { name: 'Default destination crew' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Crew a' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.default_destination', 'a'))
  })

  it('shows why a save failed', async () => {
    mocks.patchConfig.mockRejectedValue(new Error('config is read-only'))
    renderPicker([inst('a')], '')
    fireEvent.click(screen.getByRole('button', { name: 'Default destination crew' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Crew a' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('config is read-only')
  })
})
