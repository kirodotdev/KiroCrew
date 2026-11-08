/**
 * The Board's Pause all / Resume all row and a stopped member's own Continue:
 * each calls its route and nothing else, Pause all only after the confirm.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { api } from '../../../api/client'
import { CrewMemberContinue, CrewPauseRow } from './CrewPauseControl'
import { CrewRowAttention } from '../../chat-sidebar/CrewRowCounts'

const answer = vi.hoisted(() => ({ value: true }))
vi.mock('../../../components/ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: async () => answer.value, confirmDialog: null, cancel: () => {}, confirmOpen: false }),
}))

function show(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('CrewPauseRow', () => {
  beforeEach(() => {
    answer.value = true
    vi.restoreAllMocks()
    vi.spyOn(api, 'crewPause').mockResolvedValue({ ok: true, paused: true, stopped: ['w1'] })
    vi.spyOn(api, 'crewResume').mockResolvedValue({ ok: true, paused: false, resumed: {} })
  })

  it('counts what runs and needs the person, and pauses after the confirm', async () => {
    show(<CrewPauseRow root="r" paused={undefined} running={3} needsYou={1} />)
    expect(screen.getByTestId('crew-pause-summary').textContent).toBe('3 running · 1 needs you')
    fireEvent.click(screen.getByTestId('crew-pause-all'))
    await waitFor(() => expect(api.crewPause).toHaveBeenCalledWith('r'))
  })

  it('pauses nothing when the person cancels', async () => {
    answer.value = false
    show(<CrewPauseRow root="r" paused={undefined} running={2} needsYou={0} />)
    fireEvent.click(screen.getByTestId('crew-pause-all'))
    await waitFor(() => expect(screen.getByTestId('crew-pause-all')).not.toBeDisabled())
    expect(api.crewPause).not.toHaveBeenCalled()
  })

  it('shows the paused row and resumes', async () => {
    show(<CrewPauseRow root="r" paused={{ stopped: ['w1', 'w2'], paused_at: '' }} running={0} needsYou={0} />)
    expect(screen.getByTestId('crew-pause-row').dataset.paused).toBe('true')
    expect(screen.getByTestId('crew-pause-summary').textContent).toBe('Paused · 2 workers stopped · queued messages kept')
    fireEvent.click(screen.getByTestId('crew-resume-all'))
    await waitFor(() => expect(api.crewResume).toHaveBeenCalledWith('r'))
  })

  it('says so when a session could not be handed back', async () => {
    vi.mocked(api.crewResume).mockResolvedValue({ ok: true, paused: true, resumed: { w1: 'slot_subagents_running' }, failed: ['w1'] })
    show(<CrewPauseRow root="r" paused={{ stopped: ['w1'], paused_at: '' }} running={0} needsYou={0} />)
    fireEvent.click(screen.getByTestId('crew-resume-all'))
    await waitFor(() => expect(screen.getByText('Could not resume the crew.')).toBeTruthy())
  })

  it('offers Pause all while the lead alone works', async () => {
    show(<CrewPauseRow root="r" paused={undefined} running={0} needsYou={0} leadRunning />)
    expect(screen.getByTestId('crew-pause-summary').textContent).toBe('Lead working')
    fireEvent.click(screen.getByTestId('crew-pause-all'))
    await waitFor(() => expect(api.crewPause).toHaveBeenCalledWith('r'))
  })

  it('is not drawn when nothing runs and nothing is paused', () => {
    show(<CrewPauseRow root="r" paused={undefined} running={0} needsYou={2} />)
    expect(screen.queryByTestId('crew-pause-row')).toBeNull()
  })
})

describe('CrewMemberContinue', () => {
  it("continues the member through the tab's own Continue route", async () => {
    const cont = vi.spyOn(api, 'continueSlot').mockResolvedValue({ ok: true } as never)
    show(<CrewMemberContinue root="r" worker="w1" />)
    expect(screen.getByTestId('crew-member-paused').textContent).toContain('Paused')
    fireEvent.click(screen.getByTestId('crew-member-continue'))
    await waitFor(() => expect(cont).toHaveBeenCalledWith('w1'))
  })
})

describe('CrewRowAttention while paused', () => {
  it('says Paused after what needs the person, and drops the running count', () => {
    render(<CrewRowAttention needsYou={1} stalled={0} running={2} done={false} paused title="t" />)
    expect(screen.getByTestId('crew-root-paused').textContent).toBe('Paused')
    const counts = screen.getAllByTestId('crew-root-count')
    expect(counts.map(c => c.dataset.bucket)).toEqual(['needs_you'])
    expect(counts[0].textContent).toBe('1 needs you')
  })

  it('says only Paused when nothing needs the person', () => {
    render(<CrewRowAttention needsYou={0} stalled={0} running={2} done={false} paused title="t" />)
    expect(screen.getByTestId('crew-root-paused').textContent).toBe('Paused')
    expect(screen.queryByTestId('crew-root-count')).toBeNull()
  })
})
