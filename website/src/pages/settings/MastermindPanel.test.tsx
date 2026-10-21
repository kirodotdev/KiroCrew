/**
 * Settings > Mastermind: the switch calls the backend, the memory browser zooms
 * line by line down to a message's text, and the two destructive actions go
 * through the confirm dialog to the right endpoints.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({
  mastermind: vi.fn(),
  mastermindView: vi.fn(),
  mastermindBrowse: vi.fn(),
  setMastermindEnabled: vi.fn(),
  clearMastermind: vi.fn(),
  removeMastermind: vi.fn(),
  mastermindExport: vi.fn(),
  chatSlots: vi.fn(),
  kirocrewConfig: vi.fn(),
  patchConfig: vi.fn(),
  models: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api }))
vi.mock('@radix-ui/react-select', async () => await import('../../test/__mocks__/@radix-ui/react-select'))

import { MastermindPanel } from './MastermindPanel'

const INFO = { enabled: false, slot_exists: false, messages: 1234, pending_summaries: 2, view_bytes: 2500, view_lines: 3 }
const VIEW = {
  count: 3,
  lines: [
    { id: 1, n: 4, ref: '1+4', text: 'Planning the launch', built: true },
    { id: 5, n: 1, ref: '5+1', text: 'Ship it on Friday', built: true },
    { id: 6, n: 2, ref: '6+2', text: '', built: false },
  ],
}

function renderPanel() {
  const store = createTestStore({})
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MastermindPanel />
      </Provider>
    </QueryClientProvider>,
  )
}

/** Both reads land before a test asserts on either. */
async function ready() {
  await screen.findByTestId('mastermind-stats')
  await screen.findByTestId('mastermind-browser')
}

beforeEach(() => {
  api.mastermind.mockResolvedValue(INFO)
  api.mastermindView.mockResolvedValue(VIEW)
  api.setMastermindEnabled.mockResolvedValue({ enabled: true })
  api.clearMastermind.mockResolvedValue({ ok: true })
  api.removeMastermind.mockResolvedValue({ ok: true })
  api.chatSlots.mockResolvedValue([])
  api.kirocrewConfig.mockResolvedValue({ agent: { role_models: { background: 'model-large' } } })
  api.patchConfig.mockResolvedValue({})
  api.models.mockResolvedValue([{ model_name: 'auto' }, { model_name: 'model-small' }, { model_name: 'model-large' }])
  api.mastermindBrowse.mockImplementation((id: number, n: number, at?: number) => {
    if (id === 1 && n === 4) return Promise.resolve({ text: '1+2|First half\n3+2|Second half' })
    if (id === 1 && n === 2) return Promise.resolve({ text: '1+1|Hello there\n2+1|General Kenobi' })
    if (id === 1 && n === 1 && !at) return Promise.resolve({ text: 'Hello there, the opening line (zoom with at=200)' })
    if (id === 1 && n === 1 && at === 200) return Promise.resolve({ text: 'and the rest of it' })
    return Promise.resolve({ text: '' })
  })
})
afterEach(() => vi.clearAllMocks())

describe('Settings > Mastermind', () => {
  it('shows the stats and the view, with unbuilt lines muted', async () => {
    renderPanel()
    await ready()
    const stats = screen.getByTestId('mastermind-stats')
    expect(stats).toHaveTextContent('1,234')
    expect(stats).toHaveTextContent('2.5')
    const unbuilt = screen.getByTestId('mastermind-line-6+2')
    expect(unbuilt).toHaveAttribute('data-built', 'false')
    expect(unbuilt).toHaveTextContent('not summarized yet')
  })

  it('the Enabled switch posts the new state', async () => {
    renderPanel()
    await ready()
    const toggle = screen.getByRole('switch', { name: 'Enabled' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(toggle)
    await waitFor(() => expect(api.setMastermindEnabled).toHaveBeenCalledWith(true))
    // The status is re-read so the sidebar and this page see the new state.
    await waitFor(() => expect(api.mastermind).toHaveBeenCalledTimes(2))
  })

  it('zooms recursively down to a message, and pages its text', async () => {
    renderPanel()
    await ready()
    fireEvent.click(within(screen.getByTestId('mastermind-line-1+4')).getByRole('button', { name: 'Zoom into 1+4' }))
    expect(await screen.findByTestId('mastermind-line-1+2')).toHaveTextContent('First half')
    expect(screen.getByTestId('mastermind-line-3+2')).toHaveTextContent('Second half')
    expect(api.mastermindBrowse).toHaveBeenCalledWith(1, 4)

    fireEvent.click(within(screen.getByTestId('mastermind-line-1+2')).getByRole('button', { name: 'Zoom into 1+2' }))
    expect(await screen.findByTestId('mastermind-line-1+1')).toHaveTextContent('Hello there')

    fireEvent.click(within(screen.getByTestId('mastermind-line-1+1')).getByRole('button', { name: 'Zoom into 1+1' }))
    const message = await screen.findByTestId('mastermind-message-1')
    expect(await within(message).findByText(/the opening line/)).toBeTruthy()
    fireEvent.click(within(message).getByRole('button', { name: 'Load more' }))
    expect(await within(message).findByText('and the rest of it')).toBeTruthy()
    expect(api.mastermindBrowse).toHaveBeenCalledWith(1, 1, 200)

    // Closing a line hides its children again.
    const top = within(screen.getByTestId('mastermind-line-1+4')).getByRole('button', { name: 'Close 1+4' })
    expect(top).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(top)
    expect(top).toHaveAttribute('aria-expanded', 'false')
  })

  it('Clear memory asks first, then posts clear; Cancel posts nothing', async () => {
    renderPanel()
    await ready()
    fireEvent.click(screen.getByTestId('mastermind-clear'))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('The chat transcript stays visible')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(api.clearMastermind).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('mastermind-clear'))
    const again = await screen.findByRole('dialog')
    fireEvent.click(within(again).getByRole('button', { name: 'Clear memory' }))
    await waitFor(() => expect(api.clearMastermind).toHaveBeenCalledTimes(1))
  })

  it('Remove Mastermind posts forget=false by default and forget=true with the box ticked', async () => {
    api.mastermind.mockResolvedValue({ ...INFO, enabled: true, slot_exists: true })
    renderPanel()
    await ready()
    fireEvent.click(screen.getByTestId('mastermind-remove'))
    let dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove Mastermind' }))
    await waitFor(() => expect(api.removeMastermind).toHaveBeenCalledWith(false))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    fireEvent.click(screen.getByTestId('mastermind-remove'))
    dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByTestId('mastermind-remove-forget'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove Mastermind' }))
    await waitFor(() => expect(api.removeMastermind).toHaveBeenLastCalledWith(true))
  })

  it('a failed action is reported, not swallowed', async () => {
    api.setMastermindEnabled.mockRejectedValue(new Error('gateway said no'))
    renderPanel()
    await ready()
    fireEvent.click(screen.getByRole('switch', { name: 'Enabled' }))
    expect(await screen.findByTestId('mastermind-action-error')).toHaveTextContent('gateway said no')
  })

  it('the Summary model select writes agent.role_models.mastermind, apart from the other roles', async () => {
    renderPanel()
    await ready()
    const select = await screen.findByRole('combobox', { name: 'Summary model' })
    await waitFor(() => expect(select).not.toHaveAttribute('data-disabled'))
    // Unpinned reads as Auto -- not the background role's pin, not the chat model.
    expect(select).toHaveTextContent('Auto (provider picks)')
    fireEvent.click(select)
    const list = screen.getAllByRole('listbox').find(l => !l.closest('nav'))!
    await waitFor(() => expect(within(list).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Auto (provider picks)', 'model-small', 'model-large']))
    fireEvent.click(within(list).getByRole('option', { name: 'model-small' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.role_models.mastermind', 'model-small'))
    expect(api.patchConfig).toHaveBeenCalledTimes(1)
  })

  it('a failed summary-model save is reported', async () => {
    api.patchConfig.mockRejectedValue(new Error('nope'))
    renderPanel()
    await ready()
    const select = await screen.findByRole('combobox', { name: 'Summary model' })
    await waitFor(() => expect(select).not.toHaveAttribute('data-disabled'))
    fireEvent.click(select)
    const list = screen.getAllByRole('listbox').find(l => !l.closest('nav'))!
    fireEvent.click(await within(list).findByRole('option', { name: 'model-large' }))
    expect(await screen.findByTestId('mastermind-summary-model-error')).toHaveTextContent('Could not save the summary model')
  })
})
