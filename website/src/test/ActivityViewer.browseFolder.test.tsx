/**
 * The chat panel's Artifacts tab links to the library scoped to this chat and
 * to the chat's sidebar folder.
 *
 * "Browse all" opens the whole library. When the chat is filed in a sidebar
 * folder, a "Browse this folder" link sits beside it and opens the library with
 * `?chat_folder=<id>`, which the server resolves to every chat in that folder
 * and its sub-folders. An unfiled chat shows only "Browse all". The
 * "This session" header's "View in Artifacts" opens `?touched_by=<slot>`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

vi.mock('../api/client', () => ({
  api: {
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifactSessionDocs: vi.fn().mockResolvedValue({ docs: [] }),
    setArtifactPinned: vi.fn().mockResolvedValue({}),
  },
}))

import ActivityViewer from '../pages/chat/ActivityViewer'
import { api } from '../api/client'

const LIBRARY = [{ slug: 'older', name: 'From Last Week', kind: 'markdown', pinned: true }]
const TOUCHED = [{ slug: 'mine', name: 'Made Here', kind: 'widget', pinned: true }]

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>
}

function renderTab(slots: unknown[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(sseSlots(slots as never))
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/chat']}>
          <ActivityViewer
            subagents={{}}
            toolLog={[]}
            open
            onToggle={vi.fn()}
            slot="test-slot"
            view="artifacts"
          />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ActivityViewer Artifacts tab: Browse this folder', () => {
  beforeEach(() => {
    vi.mocked(api.artifacts).mockImplementation((filters?: { touchedBy?: string }) =>
      Promise.resolve({ artifacts: filters?.touchedBy ? TOUCHED : LIBRARY }) as never,
    )
  })

  it('offers the folder link beside Browse all when the chat is filed', async () => {
    renderTab([{ key: 'test-slot', title: 'Ops chat', messages: 1, running: false, folder_id: 'f-ops' }])
    const folderLink = await screen.findByRole('button', { name: 'Browse this folder' })
    expect(screen.getByRole('button', { name: /Browse all \(1\)/ })).toBeInTheDocument()
    fireEvent.click(folderLink)
    expect(screen.getByTestId('location')).toHaveTextContent('/artifacts?chat_folder=f-ops')
  })

  it('keeps Browse all pointing at the whole library', async () => {
    renderTab([{ key: 'test-slot', title: 'Ops chat', messages: 1, running: false, folder_id: 'f-ops' }])
    fireEvent.click(await screen.findByRole('button', { name: /Browse all \(1\)/ }))
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/artifacts$/)
  })

  it("opens this chat's artifacts in the library from the This session header", async () => {
    renderTab([{ key: 'test-slot', title: 'Loose chat', messages: 1, running: false }])
    fireEvent.click(await screen.findByRole('button', { name: 'View in Artifacts' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/artifacts?touched_by=test-slot')
  })

  it('shows only Browse all for a chat that is not filed in a folder', async () => {
    renderTab([{ key: 'test-slot', title: 'Loose chat', messages: 1, running: false }])
    expect(await screen.findByRole('button', { name: /Browse all \(1\)/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Browse this folder' })).not.toBeInTheDocument()
  })
})
