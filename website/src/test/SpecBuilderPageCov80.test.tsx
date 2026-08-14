// SpecBuilderPage shell — selection persistence, the stale-selection guard, the
// error banner, and the create / settings view swaps. The three child views are
// stubbed so this exercises the page's own plumbing rather than their internals.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { SpecSummary } from '../apps/spec-builder/api'

const list = vi.fn()

vi.mock('../apps/spec-builder/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/spec-builder/api')>()
  return { ...actual, specApi: { ...actual.specApi, list: () => list() } }
})

interface WorkspaceStub {
  specs: SpecSummary[]
  sel: string | null
  setSel: (n: string | null) => void
  setErr: (m: string) => void
  onNew: () => void
  loading: boolean
  onSettings: () => void
}

vi.mock('../apps/spec-builder/components/Workspace', () => ({
  default: (p: WorkspaceStub) => (
    <div data-testid="workspace">
      <span data-testid="sel">{p.sel ?? '(none)'}</span>
      <span data-testid="names">{p.specs.map(s => s.name).join(',')}</span>
      <span data-testid="loading">{String(p.loading)}</span>
      <button onClick={() => p.setSel('zz-one')}>zz-select-one</button>
      <button onClick={() => p.setSel('zz-ghost')}>zz-select-ghost</button>
      <button onClick={() => p.setSel(null)}>zz-clear-selection</button>
      <button onClick={() => p.setErr('zz-workspace-error')}>zz-raise-error</button>
      <button onClick={p.onNew}>zz-new</button>
      <button onClick={p.onSettings}>zz-open-settings</button>
    </div>
  ),
}))

vi.mock('../apps/spec-builder/components/NewSpecView', () => ({
  default: (p: { onCancel: () => void; onCreated: (n: string) => void; onSettings: () => void }) => (
    <div data-testid="new-spec-view">
      <button onClick={p.onCancel}>zz-cancel-new</button>
      <button onClick={() => p.onCreated('zz-two')}>zz-finish-new</button>
      <button onClick={p.onSettings}>zz-new-settings</button>
    </div>
  ),
}))

vi.mock('../apps/spec-builder/components/SettingsModal', () => ({
  default: (p: { onClose: () => void }) => (
    <div data-testid="settings-modal"><button onClick={p.onClose}>zz-close-settings</button></div>
  ),
}))

import SpecBuilderPage from '../apps/spec-builder/SpecBuilderPage'
import { LS } from '../apps/spec-builder/api'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SpecBuilderPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const workspace = () => screen.findByTestId('workspace')

beforeEach(() => {
  localStorage.clear()
  list.mockReset().mockResolvedValue({ specs: [{ name: 'zz-one', phase: 'requirements' }] })
})

afterEach(() => { vi.restoreAllMocks() })

describe('SpecBuilderPage selection', () => {
  it('hands the loaded specs and the loading flag to the workspace', async () => {
    renderPage()
    await workspace()
    await waitFor(() => expect(screen.getByTestId('names')).toHaveTextContent('zz-one'))
    expect(screen.getByTestId('loading')).toHaveTextContent('false')
  })

  it('persists a selection so the next visit restores it', async () => {
    renderPage()
    await workspace()
    fireEvent.click(screen.getByText('zz-select-one'))
    expect(screen.getByTestId('sel')).toHaveTextContent('zz-one')
    expect(localStorage.getItem(LS.lastOpen)).toBe('zz-one')
  })

  it('restores the persisted selection on mount', async () => {
    localStorage.setItem(LS.lastOpen, 'zz-one')
    renderPage()
    await workspace()
    expect(screen.getByTestId('sel')).toHaveTextContent('zz-one')
  })

  it('forgets the selection when it is cleared', async () => {
    localStorage.setItem(LS.lastOpen, 'zz-one')
    renderPage()
    await workspace()
    fireEvent.click(screen.getByText('zz-clear-selection'))
    expect(localStorage.getItem(LS.lastOpen)).toBeNull()
    expect(screen.getByTestId('sel')).toHaveTextContent('(none)')
  })

  it('drops a restored selection that no longer exists on the server', async () => {
    localStorage.setItem(LS.lastOpen, 'zz-deleted-elsewhere')
    renderPage()
    await workspace()
    await waitFor(() => expect(screen.getByTestId('sel')).toHaveTextContent('(none)'))
  })

  it('survives a storage backend that refuses reads and writes', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('private mode') })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('private mode') })
    renderPage()
    await workspace()
    expect(screen.getByTestId('sel')).toHaveTextContent('(none)')
    fireEvent.click(screen.getByText('zz-select-one'))
    expect(screen.getByTestId('sel')).toHaveTextContent('zz-one')
    getItem.mockRestore()
    setItem.mockRestore()
  })
})

describe('SpecBuilderPage error banner', () => {
  it('announces a list failure and can be dismissed', async () => {
    list.mockRejectedValue(new Error('zz-list-failed'))
    renderPage()
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('zz-list-failed')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('surfaces an error raised by a child view', async () => {
    renderPage()
    await workspace()
    fireEvent.click(screen.getByText('zz-raise-error'))
    expect(await screen.findByRole('alert')).toHaveTextContent('zz-workspace-error')
  })
})

describe('SpecBuilderPage view swaps', () => {
  it('swaps to the creator and back on cancel', async () => {
    renderPage()
    await workspace()
    fireEvent.click(screen.getByText('zz-new'))
    expect(screen.getByTestId('new-spec-view')).toBeInTheDocument()
    expect(screen.queryByTestId('workspace')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('zz-cancel-new'))
    expect(await workspace()).toBeInTheDocument()
  })

  // NOTE: creating a spec should also SELECT it, but the stale-selection guard
  // races the list invalidation and drops `sel` before the refetch lands (see
  // the bug reported alongside this suite). That is deliberately NOT asserted
  // here in either direction — only the list refresh, which is correct today.
  it('refreshes the specs list after one is created', async () => {
    renderPage()
    await workspace()
    await waitFor(() => expect(screen.getByTestId('names')).toHaveTextContent('zz-one'))
    list.mockResolvedValue({ specs: [{ name: 'zz-one' }, { name: 'zz-two' }] })
    fireEvent.click(screen.getByText('zz-new'))
    fireEvent.click(screen.getByText('zz-finish-new'))
    expect(screen.queryByTestId('new-spec-view')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('names')).toHaveTextContent('zz-one,zz-two'))
  })

  it('opens and closes the settings modal from the workspace', async () => {
    renderPage()
    await workspace()
    fireEvent.click(screen.getByText('zz-open-settings'))
    expect(screen.getByTestId('settings-modal')).toBeInTheDocument()
    fireEvent.click(screen.getByText('zz-close-settings'))
    expect(screen.queryByTestId('settings-modal')).not.toBeInTheDocument()
  })

  it('opens the settings modal from the creator too', async () => {
    renderPage()
    await workspace()
    fireEvent.click(screen.getByText('zz-new'))
    fireEvent.click(screen.getByText('zz-new-settings'))
    expect(screen.getByTestId('settings-modal')).toBeInTheDocument()
  })
})
