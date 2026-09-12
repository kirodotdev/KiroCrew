/**
 * The model dropdown's "Model order" drill-in page (PR #9969, relocated from a
 * Settings card on maintainer review). Three things must hold: the footer row
 * opens the editor page; the editor participates in the same read-only config
 * gate it carried as a card (a pending config read must not render a drag
 * surface); and the host's list-navigation keyboard handler is OFF on the
 * order page so dnd-kit's keyboard sensor owns the arrows.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'
import { render } from '@testing-library/react'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

const { kirocrewConfigMock } = vi.hoisted(() => ({ kirocrewConfigMock: vi.fn() }))
vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: kirocrewConfigMock,
    patchConfig: vi.fn(),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({ ok: true }),
  },
}))
vi.mock('../hooks/useAvailableModels', () => ({
  useModelOrderLoadFailed: () => false,
  useAvailableModels: () => [
    { name: 'auto', description: '' },
    { name: 'opus', description: 'Opus' },
    { name: 'sonnet', description: 'Sonnet' },
  ],
}))
vi.mock('../components/ReasoningEffortDropdown', () => ({
  default: () => <div data-testid="effort-dropdown" />,
}))

const onListKeyDown = vi.fn()
const baseProps = {
  anchorRect: { right: 400, top: 300 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: [{ name: 'auto', description: 'Default' }, { name: 'opus' }, { name: 'sonnet' }],
  activeModel: 'auto',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: false,
  slot: 'dashboard:1',
  currentEffort: '',
  onListKeyDown,
}

function wrap(overrides: Partial<React.ComponentProps<typeof ModelEffortDropdown>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ModelEffortDropdown {...baseProps} {...overrides} />
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ModelEffortDropdown — Model order page', () => {
  beforeEach(() => {
    onListKeyDown.mockClear()
    kirocrewConfigMock.mockResolvedValue({ agent: { model_order: [] } })
  })

  it('opens the editor from the footer row', async () => {
    wrap()
    fireEvent.click(screen.getByText('Model order'))
    // The editor's description line renders on the order page.
    expect(await screen.findByText(/Drag to set the order/)).toBeInTheDocument()
  })

  it('editor stays read-only while the config read is pending (gate carried over)', async () => {
    kirocrewConfigMock.mockImplementation(() => new Promise(() => {}))
    wrap()
    fireEvent.click(screen.getByText('Model order'))
    expect(await screen.findByText(/Drag to set the order/)).toBeInTheDocument()
    expect(screen.queryByText(/Reset to default order/)).not.toBeInTheDocument()
  })

  it('host list-nav keyboard handler is off on the order page', async () => {
    const { container } = wrap()
    const root = container.querySelector('.fixed') as HTMLElement
    fireEvent.keyDown(root, { key: 'ArrowDown' })
    expect(onListKeyDown).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('Model order'))
    await screen.findByText(/Drag to set the order/)
    fireEvent.keyDown(root, { key: 'ArrowDown' })
    // Still exactly one call: the order page detached the host handler.
    expect(onListKeyDown).toHaveBeenCalledTimes(1)
  })

  it('fits a 320px viewport with 8px gutters (narrow-viewport-required)', () => {
    const prev = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 320, configurable: true })
    try {
      const { container } = wrap()
      const root = container.querySelector('.fixed') as HTMLElement
      // 320 - 16 gutters = 304; pinned left gutter of 8 keeps the box inside.
      expect(root.style.width).toBe('304px')
      expect(root.style.left).toBe('8px')
    } finally {
      Object.defineProperty(window, 'innerWidth', { value: prev, configurable: true })
    }
  })

  it('keeps the 340px design width on wide viewports', () => {
    const { container } = wrap()
    const root = container.querySelector('.fixed') as HTMLElement
    expect(root.style.width).toBe('340px')
  })

  it('opens the reasoning-effort page from the footer and returns via the back chevron', async () => {
    const { container } = wrap({ hasEffort: true, currentEffort: 'high' })
    // Effort footer renders only when hasEffort && slot (covers the footer branch).
    fireEvent.click(screen.getByText('Reasoning'))
    // Page 2 hosts the embedded effort dropdown behind the back chevron.
    expect(await screen.findByTestId('effort-dropdown')).toBeInTheDocument()
    // Host list-nav handler is off on the effort page too.
    const root = container.querySelector('.fixed') as HTMLElement
    fireEvent.keyDown(root, { key: 'ArrowDown' })
    expect(onListKeyDown).not.toHaveBeenCalled()
    // Back chevron returns to the list page (both back buttons render one per
    // drill-in page; either returns to 'list', so click the first).
    fireEvent.click(screen.getAllByText('Models')[0])
    fireEvent.keyDown(root, { key: 'ArrowDown' })
    expect(onListKeyDown).toHaveBeenCalledTimes(1)
  })
})
