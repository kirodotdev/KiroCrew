/**
 * Conductor mode Board: the Pause all / Resume all row on a crew whose root runs
 * conductor mode's default agent. The row reads no agent name; this pins that.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewPause: vi.fn(), crewResume: vi.fn(), crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))
vi.mock('../../../components/ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: async () => true, confirmDialog: null, cancel: () => {}, confirmOpen: false }),
}))
vi.mock('../../../hooks/usePreviewFlag', async importOriginal => ({
  ...(await importOriginal<typeof import('../../../hooks/usePreviewFlag')>()),
  useCrewMode: () => true,
}))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import { DEFAULT_CREW_MODE_AGENT } from '../../../lib/crewMode'

const ROOT = { key: 'root-1', title: 'Main', agent: DEFAULT_CREW_MODE_AGENT, agent_kind: 'template', crew_mode_root: true, running: true }
const WORKER = { key: 'w-1', title: 'Worker', agent: 'kirocrew-worker', agent_kind: 'template', crew_root: 'root-1', running: true }

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [ROOT, WORKER] } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active /></Provider></QueryClientProvider>)
  // The row sits on the Workers list, the panel's second view.
  fireEvent.click(screen.getByTestId('conductor-mode-view-list'))
}

describe('Conductor mode Pause all on the default conductor', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.crewBoard.mockResolvedValue({ conductor: null, conductor_alive: 'idle', items: [], take_over_available: false })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
    api.crewPause.mockResolvedValue({ ok: true, paused: true, stopped: ['w-1'] })
    api.crewResume.mockResolvedValue({ ok: true, paused: false, resumed: {} })
  })
  afterEach(() => vi.clearAllMocks())

  it('offers Pause all and calls the pause route for the root', async () => {
    expect(DEFAULT_CREW_MODE_AGENT).toBe('kirocrew-conductor')
    renderPanel()
    fireEvent.click(await screen.findByTestId('crew-pause-all'))
    await waitFor(() => expect(api.crewPause).toHaveBeenCalledWith('root-1'))
  })

  it('offers Resume all on a paused crew and calls the resume route', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [], paused: { stopped: ['w-1'], paused_at: '' } })
    renderPanel()
    fireEvent.click(await screen.findByTestId('crew-resume-all'))
    await waitFor(() => expect(api.crewResume).toHaveBeenCalledWith('root-1'))
  })
})
