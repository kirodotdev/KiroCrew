import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import BotSessionListSwitches from './BotSessionListSwitches'
import { ShowAllBotsSessionsRow } from '../settings/ShowAllBotsSessionsRow'

/** The two switches that keep Bot sessions in the person's Sessions list: the
 *  global one lists every Bot's and, while on, forces this Bot's on and locks it;
 *  while it is off, this Bot's own switch decides. */

const mockApi = vi.hoisted(() => ({
  kirocrewConfig: vi.fn(),
  updateDashboardConfig: vi.fn(),
  updateKirocrewAgent: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api: mockApi }))

const ALL = "Show all bots in my Sessions"
const THIS = "Show Ada in my Sessions"

function setup(everyone: boolean, own: boolean) {
  mockApi.kirocrewConfig.mockResolvedValue({ dashboard: { show_bot_sessions_in_list: everyone } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <BotSessionListSwitches member={{ name: 'ada', show_sessions_in_list: own }} displayName="Ada" />
    </QueryClientProvider>,
  )
}

/** The global switch once the config read has landed (Toggle marks a switch it ignores with aria-disabled). */
const loadedAll = async () => {
  const sw = screen.getByRole('switch', { name: ALL })
  await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
  return sw
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.updateDashboardConfig.mockResolvedValue({ ok: true })
  mockApi.updateKirocrewAgent.mockResolvedValue({ ok: true })
})

describe('BotSessionListSwitches', () => {
  it("with the global switch off, this Bot's switch shows its own value and saves on the Bot", async () => {
    setup(false, false)
    expect(await loadedAll()).toHaveAttribute('aria-checked', 'false')
    const own = screen.getByRole('switch', { name: THIS })
    expect(own).not.toHaveAttribute('aria-disabled')
    expect(own).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(own)
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('ada', { show_sessions_in_list: true }))
    expect(mockApi.updateDashboardConfig).not.toHaveBeenCalled()
  })

  it("with the global switch on, this Bot's reads on and cannot be pressed, whatever it saved", async () => {
    setup(true, false)
    const all = await loadedAll()
    await waitFor(() => expect(all).toHaveAttribute('aria-checked', 'true'))
    const own = screen.getByRole('switch', { name: THIS })
    expect(own).toHaveAttribute('aria-checked', 'true')
    expect(own).toHaveAttribute('aria-disabled', 'true')
  })

  it("turning the global switch on saves the dashboard setting and locks this Bot's on", async () => {
    setup(false, false)
    fireEvent.click(await loadedAll())
    await waitFor(() => expect(mockApi.updateDashboardConfig).toHaveBeenCalledWith({ show_bot_sessions_in_list: true }))
    await waitFor(() => expect(screen.getByRole('switch', { name: THIS })).toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByRole('switch', { name: THIS })).toHaveAttribute('aria-checked', 'true')
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('says a failed config read and retries it in place', async () => {
    mockApi.kirocrewConfig.mockRejectedValueOnce(new Error('down'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <BotSessionListSwitches member={{ name: 'ada', show_sessions_in_list: false }} displayName="Ada" />
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('bot-sessions-in-list-load-error')).toBeInTheDocument()
    mockApi.kirocrewConfig.mockResolvedValue({ dashboard: { show_bot_sessions_in_list: true } })
    fireEvent.click(screen.getByTestId('bot-sessions-in-list-retry'))
    await waitFor(() => expect(screen.queryByTestId('bot-sessions-in-list-load-error')).toBeNull())
    expect(screen.getByRole('switch', { name: ALL })).toHaveAttribute('aria-checked', 'true')
  })

  it('the Settings row and the list read one value, and follow a refresh from another dashboard', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ dashboard: { show_bot_sessions_in_list: false } })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ShowAllBotsSessionsRow />
        <BotSessionListSwitches member={{ name: 'ada', show_sessions_in_list: false }} displayName="Ada" />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getAllByRole('switch', { name: ALL })[0]).not.toHaveAttribute('aria-disabled'))
    // Another dashboard turned it on: its refresh frame re-reads the config query.
    mockApi.kirocrewConfig.mockResolvedValue({ dashboard: { show_bot_sessions_in_list: true } })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => {
      for (const sw of screen.getAllByRole('switch', { name: ALL })) expect(sw).toHaveAttribute('aria-checked', 'true')
    })
  })

  it('says a save failed', async () => {
    setup(false, false)
    mockApi.updateKirocrewAgent.mockRejectedValue(new Error('nope'))
    await loadedAll()
    fireEvent.click(screen.getByRole('switch', { name: THIS }))
    expect(await screen.findByTestId('bot-sessions-in-list-error')).toBeInTheDocument()
  })
})
