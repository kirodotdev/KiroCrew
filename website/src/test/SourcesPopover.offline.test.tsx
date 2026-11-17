/**
 * SourcesPopover — every registry and install write is refused while the
 * gateway is offline (#10463): Install from path (and Enter in its field), the
 * add-registry submit (and Enter in its fields), each row's Refresh and Remove,
 * and Sync apps dim, say why in their own tooltip, and send no request, the
 * same refusal the Library and Discover pages use. Opening the add form and the
 * repository link stay live.
 *
 * Driven through the real popover and the real RegistryManager; a refused
 * control is proven by its api mock never being called, and the connected
 * twin of each control proves the same gesture does write.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createConnectedTestStore, createTestStore } from './helpers'

const mocks = vi.hoisted(() => ({
  listRegistries: vi.fn(),
  updateRegistries: vi.fn(),
  refreshRegistries: vi.fn(),
  installApp: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    listRegistries: (...a: unknown[]) => mocks.listRegistries(...a),
    updateRegistries: (...a: unknown[]) => mocks.updateRegistries(...a),
    refreshRegistries: (...a: unknown[]) => mocks.refreshRegistries(...a),
    installApp: (...a: unknown[]) => mocks.installApp(...a),
  },
}))
vi.mock('../rum', () => ({ recordEvent: vi.fn() }))

import SourcesPopover from '../components/appstore/SourcesPopover'

const OFFLINE_TITLE = 'Gateway offline — reconnect to manage apps'
const REG = { name: 'demo', repo: 'https://github.com/example/demo-registry', branch: 'main', trust: 'index' }

function renderPopover(store: ReturnType<typeof createTestStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SourcesPopover open onOpenChange={() => {}} onError={() => {}} />
      </QueryClientProvider>
    </Provider>,
  )
}

const pathInput = () => screen.getByRole('textbox', { name: 'App directory path' })

/** Opens the add form (a live control) and fills a valid repo. */
async function openAddForm() {
  fireEvent.click(await screen.findByRole('button', { name: 'Add Registry' }))
  fireEvent.change(screen.getByLabelText('Repo *'), { target: { value: 'https://github.com/example/other' } })
}

function expectRefused(el: HTMLElement) {
  expect(el).toHaveAttribute('aria-disabled', 'true')
  expect(el).toHaveAttribute('title', OFFLINE_TITLE)
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listRegistries.mockResolvedValue({ registries: [REG], pinned: [] })
  mocks.updateRegistries.mockResolvedValue({ ok: true })
  mocks.refreshRegistries.mockResolvedValue({ ok: true, refreshed: [], failed: [], results: [], apps: 0, lastSyncedAt: '' })
  mocks.installApp.mockResolvedValue({ name: 'my-app' })
})

describe('SourcesPopover offline', () => {
  it('Install from path is inert on click and Enter, says why, and installs nothing', async () => {
    renderPopover(createTestStore())
    fireEvent.change(pathInput(), { target: { value: '/home/u/apps/my-app' } })
    const install = screen.getByRole('button', { name: 'Install disabled — gateway offline' })
    expect(install).toBeDisabled()
    expectRefused(install)
    fireEvent.click(install)
    fireEvent.keyDown(pathInput(), { key: 'Enter' })
    expect(mocks.installApp).not.toHaveBeenCalled()
  })

  it("each row's Refresh and Remove, and Sync apps, are inert, say why, and write nothing", async () => {
    renderPopover(createTestStore())
    const refresh = await screen.findByRole('button', { name: 'Refresh demo registry disabled — gateway offline' })
    const remove = screen.getByRole('button', { name: 'Remove demo registry disabled — gateway offline' })
    const sync = screen.getByRole('button', { name: 'Sync registry apps disabled — gateway offline' })
    for (const el of [refresh, remove, sync]) expectRefused(el)
    expect(sync).toBeDisabled()
    fireEvent.click(refresh)
    fireEvent.keyDown(refresh, { key: 'Enter' })
    fireEvent.click(remove)
    fireEvent.keyDown(remove, { key: 'Enter' })
    fireEvent.click(sync)
    expect(mocks.refreshRegistries).not.toHaveBeenCalled()
    expect(mocks.updateRegistries).not.toHaveBeenCalled()
  })

  it('the add form opens, but its submit and Enter in its fields add nothing', async () => {
    renderPopover(createTestStore())
    await openAddForm()
    const submit = screen.getByRole('button', { name: 'Add Registry disabled — gateway offline' })
    expect(submit).toBeDisabled()
    expectRefused(submit)
    fireEvent.click(submit)
    fireEvent.keyDown(screen.getByLabelText('Repo *'), { key: 'Enter' })
    fireEvent.keyDown(screen.getByLabelText('Branch'), { key: 'Enter' })
    expect(mocks.updateRegistries).not.toHaveBeenCalled()
    // A refused add is silent: no validation error stands in for the reason.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('the repository link stays live', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    renderPopover(createTestStore())
    fireEvent.click(await screen.findByRole('button', { name: `Open ${REG.repo} repository` }))
    expect(open).toHaveBeenCalledTimes(1)
    open.mockRestore()
  })
})

describe('SourcesPopover connected', () => {
  it('Install from path installs', async () => {
    renderPopover(createConnectedTestStore())
    fireEvent.change(pathInput(), { target: { value: '/home/u/apps/my-app' } })
    const install = screen.getByRole('button', { name: 'Install' })
    expect(install).not.toHaveAttribute('title')
    fireEvent.keyDown(pathInput(), { key: 'Enter' })
    await waitFor(() => expect(mocks.installApp).toHaveBeenCalledWith('/home/u/apps/my-app'))
  })

  it("a row's Refresh, Sync apps and Remove write", async () => {
    renderPopover(createConnectedTestStore())
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh demo registry' }))
    await waitFor(() => expect(mocks.refreshRegistries).toHaveBeenCalledWith(REG.repo))
    fireEvent.click(await screen.findByRole('button', { name: 'Sync registry apps' }))
    await waitFor(() => expect(mocks.refreshRegistries).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'Remove demo registry' }))
    await waitFor(() => expect(mocks.updateRegistries).toHaveBeenCalledWith([]))
  })

  it('Enter in the repo field adds the registry', async () => {
    renderPopover(createConnectedTestStore())
    await openAddForm()
    fireEvent.keyDown(screen.getByLabelText('Repo *'), { key: 'Enter' })
    await waitFor(() => expect(mocks.updateRegistries).toHaveBeenCalledTimes(1))
  })
})
