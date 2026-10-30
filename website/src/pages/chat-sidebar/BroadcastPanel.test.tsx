import type { ReactElement } from 'react'
import { Provider } from 'react-redux'
import { fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import type { Slot } from './types'
import type { LaneSignals } from './board'

const sendTurn = vi.hoisted(() => vi.fn())

vi.mock('../../chat-core/transport/sendTurn', () => ({ sendTurn }))

import BroadcastPanel from './BroadcastPanel'
import { fmtRelative } from '../../i18n/format'
import { store } from '../../store'

/** The panel reads the live message-recency overlay from the store. */
const render = (ui: ReactElement) => rtlRender(<Provider store={store}>{ui}</Provider>)

const slots: Slot[] = [
  { key: 'loop-busy', title: 'Babysit', running: true },
  { key: 'loop-idle', title: 'Runbook', running: false },
  { key: 'crew-loop', title: 'Astra', running: true, agent_kind: 'member' },
  { key: 'plain', title: 'Plain', running: false },
  // A crewmate chat with no kind stamp, recognised only through the roster.
  { key: 'kindless', title: 'Kindless', running: true, agent: 'astra' },
]
const agents = [{ name: 'astra', source: 'crew', selection_kind: 'member' as const }]

function setup(onClose = vi.fn(), onOpenSession = vi.fn()) {
  render(<BroadcastPanel slots={slots} folders={[]} tags={[]} agents={agents} signals={signals} onClose={onClose} onOpenSession={onOpenSession} />)
  return { onClose, onOpenSession }
}

let signals: LaneSignals
beforeEach(() => {
  signals = {
    subagentStartedCounts: {}, subagentApprovalCounts: {}, workflowActiveSet: new Set(),
    automationRunningSet: new Set(['loop-busy', 'loop-idle', 'crew-loop', 'kindless']),
  }
  sendTurn.mockReset()
})
afterEach(() => { vi.clearAllMocks() })

describe('BroadcastPanel', () => {
  it('defaults to looping sessions with mates excluded', () => {
    setup()
    expect(screen.getByTestId('broadcast-crew-exclude').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('broadcast-row-loop-busy')).toBeTruthy()
    expect(screen.getByTestId('broadcast-row-loop-idle')).toBeTruthy()
    expect(screen.queryByTestId('broadcast-row-crew-loop')).toBeNull()
    expect(screen.queryByTestId('broadcast-row-plain')).toBeNull()
    expect(screen.queryByTestId('broadcast-row-kindless')).toBeNull()
  })

  it('labels a running row as a steer and an idle row as a new message', () => {
    setup()
    expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Joins running turn')
    expect(screen.getByTestId('broadcast-row-loop-idle').textContent).toContain('Starts new turn')
  })

  it('brings mates in when included', () => {
    setup()
    fireEvent.click(screen.getByTestId('broadcast-crew-include'))
    expect(screen.getByTestId('broadcast-row-crew-loop')).toBeTruthy()
  })

  it('says a turn parked on an approval takes the message after the user answers', () => {
    signals.subagentStartedCounts = {}
    render(<BroadcastPanel slots={[{ key: 'gate', title: 'Gate', running: true, pending_approval: true }]} folders={[]} tags={[]} agents={agents} signals={signals} onClose={vi.fn()} onOpenSession={vi.fn()} />)
    fireEvent.click(screen.getByTestId('broadcast-preset-waiting'))
    expect(screen.getByTestId('broadcast-row-gate').textContent).toContain('Joins after you answer')
  })

  it('shows a folder, else the last activity, so same-named rows differ', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-15T06:00:00Z'))
    onTestFinished(() => { vi.useRealTimers() })
    render(<BroadcastPanel
      slots={[
        { key: 'a', title: 'Same', running: false, folder_id: 'f1' },
        { key: 'b', title: 'Same', running: false, last_ts: '2026-10-15T05:00:00Z' },
      ]}
      folders={[{ id: 'f1', name: 'Infra', order: 0 }]} tags={[]} agents={agents} signals={signals} onClose={vi.fn()} onOpenSession={vi.fn()} />)
    fireEvent.click(screen.getByTestId('broadcast-preset-all'))
    expect(screen.getByTestId('broadcast-row-a').textContent).toContain('Infra')
    expect(screen.getByTestId('broadcast-row-b').textContent).toContain(fmtRelative('2026-10-15T05:00:00Z', { now: Date.parse('2026-10-15T06:00:00Z') }))
  })

  it('labels an idle session with live sub-agents as waiting for that work', () => {
    signals.subagentStartedCounts = { 'loop-idle': 1 }
    setup()
    expect(screen.getByTestId('broadcast-row-loop-idle').textContent).toContain('Waits for current work')
  })

  it('keeps naming unconfirmed rows after a retry clears the refusals', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-busy' ? { status: 'refused', body: {} } : { status: 'response-late', body: {} })
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-error').textContent).toContain('1 not sent'))
    // After a send the count line is the outcome tally, not the match count.
    expect(screen.getByTestId('broadcast-count').textContent).toBe('Sent 0 · failed 1 · unconfirmed 1')
    expect(screen.getByTestId('broadcast-error').textContent).toContain('1 could not be confirmed and were not resent')
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Sent'))
    expect(screen.queryByTestId('broadcast-sent')).toBeNull()
    expect(screen.getByTestId('broadcast-error').textContent).toContain('1 could not be confirmed and were not resent')
  })

  it('keeps a delivered row ticked and locked, so a tick still means "in this broadcast"', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByLabelText('Runbook'))
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Sent'))
    const sent = screen.getByLabelText('Babysit') as HTMLInputElement
    expect(sent.checked).toBe(true)
    expect(sent.disabled).toBe(true)
    expect((screen.getByLabelText('Runbook') as HTMLInputElement).checked).toBe(false)
  })

  it('keeps Send disabled until there is text', () => {
    setup()
    expect((screen.getByTestId('broadcast-send') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: '  ' } })
    expect((screen.getByTestId('broadcast-send') as HTMLButtonElement).disabled).toBe(true)
  })

  it('steers the trimmed text into every ticked target and confirms on success', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    const { onClose } = setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: ' pause for CI ' } })
    // Untick one row: it must not be sent.
    fireEvent.click(screen.getByLabelText('Runbook'))
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-sent').textContent).toContain('Sent to 1'))
    // The panel stays open on the confirmation; Close is the user's call.
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('broadcast-close').textContent).toBe('Close')
    expect(sendTurn).toHaveBeenCalledTimes(1)
    expect(sendTurn).toHaveBeenCalledWith({ message: 'pause for CI', slot: 'loop-busy', steer: true })
  })

  it('sends an idle session a plain message, so the server hold still applies', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByLabelText('Babysit'))
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    expect(sendTurn).toHaveBeenCalledWith({ message: 'hi', slot: 'loop-idle' })
  })

  it('stays open on a refusal, never resends an unconfirmed row, and retries only the failed one', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-busy' ? { status: 'refused', body: {}, reason: 'slot is closed' } : { status: 'transport-error', body: {} })
    const { onClose } = setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Failed'))
    // A dropped connection may still have delivered: it is never resent.
    expect(screen.getByTestId('broadcast-row-loop-idle').textContent).toContain('Not confirmed')
    // The reason is shown inline under its row, not only on hover.
    expect(screen.getByTestId('broadcast-reason-loop-busy').textContent).toContain('slot is closed')
    expect(onClose).not.toHaveBeenCalled()
    sendTurn.mockClear()
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Sent'))
    expect(sendTurn.mock.calls.map(c => c[0].slot)).toEqual(['loop-busy'])
  })

  it('moves a refused row to the top of the list', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-idle' ? { status: 'refused', body: {} } : { status: 'dispatched', body: {} })
    setup()
    expect(screen.getAllByTestId(/^broadcast-row-/)[0].getAttribute('data-testid')).toBe('broadcast-row-loop-busy')
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-idle').textContent).toContain('Failed'))
    expect(screen.getAllByTestId(/^broadcast-row-/)[0].getAttribute('data-testid')).toBe('broadcast-row-loop-idle')
  })

  it('moves an unconfirmed row up too, so its open link is in view', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-idle' ? { status: 'response-late', body: {} } : { status: 'dispatched', body: {} })
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await screen.findByTestId('broadcast-open-loop-idle')
    expect(screen.getAllByTestId(/^broadcast-row-/)[0].getAttribute('data-testid')).toBe('broadcast-row-loop-idle')
  })

  it('counts every row the broadcast reached once a retry clears the last failure', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-busy' ? { status: 'refused', body: {} } : { status: 'dispatched', body: {} })
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Failed'))
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-sent').textContent).toContain('Sent to 2'))
  })

  it('keeps Cancel and the text locked while a batch is sending', async () => {
    let release: (v: unknown) => void = () => {}
    sendTurn.mockImplementation(() => new Promise(r => { release = r }))
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect((screen.getByTestId('broadcast-close') as HTMLButtonElement).disabled).toBe(true))
    expect((screen.getByTestId('broadcast-message') as HTMLTextAreaElement).disabled).toBe(true)
    release({ status: 'dispatched', body: {} })
  })

  it('sends edited text to every matched row again after a partial failure', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-busy' ? { status: 'refused', body: {} } : { status: 'dispatched', body: {} })
    setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'v1' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-row-loop-busy').textContent).toContain('Failed'))
    sendTurn.mockClear()
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'v2' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-sent')).toBeTruthy())
    expect(sendTurn.mock.calls.map(c => c[0].slot).sort()).toEqual(['loop-busy', 'loop-idle'])
  })

  it('switches to Custom when a custom state is picked', () => {
    setup()
    fireEvent.click(screen.getByTestId('broadcast-custom-toggle'))
    // The state chips start as the highlighted pick: only In a loop.
    expect(screen.getByTestId('broadcast-state-loop').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('broadcast-state-working').getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(screen.getByTestId('broadcast-state-idle'))
    expect(screen.getByTestId('broadcast-preset-custom').getAttribute('aria-pressed')).toBe('true')
    // Custom states now: loop, idle -> the idle non-crewmate row joins.
    expect(screen.getByTestId('broadcast-row-plain')).toBeTruthy()
  })

  it('re-seeds the custom state chips from every quick pick', () => {
    setup()
    fireEvent.click(screen.getByTestId('broadcast-custom-toggle'))
    fireEvent.click(screen.getByTestId('broadcast-preset-waiting'))
    expect(screen.getByTestId('broadcast-state-needs_approval').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('broadcast-state-waiting').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('broadcast-state-loop').getAttribute('aria-pressed')).toBe('false')
  })

  it('opens on the first quick pick that reaches anyone', () => {
    signals.automationRunningSet = new Set()
    render(<BroadcastPanel slots={[{ key: 'busy', title: 'Busy', running: true }, { key: 'quiet', title: 'Quiet', running: false }]}
      folders={[]} tags={[]} agents={agents} signals={signals} onClose={vi.fn()} onOpenSession={vi.fn()} />)
    // No loops, so the panel skips In a loop and opens on In progress.
    expect(screen.getByTestId('broadcast-preset-loop').getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('broadcast-preset-running').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('broadcast-row-busy')).toBeTruthy()
  })

  it('counts only the rows still listed after the pick changes', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'gate' ? { status: 'refused', body: {} } : { status: 'dispatched', body: {} })
    render(<BroadcastPanel slots={[{ key: 'gate', title: 'Gate', running: true, pending_approval: true }, { key: 'busy', title: 'Busy', running: true }]}
      folders={[]} tags={[]} agents={agents} signals={signals} onClose={vi.fn()} onOpenSession={vi.fn()} />)
    fireEvent.click(screen.getByTestId('broadcast-preset-all'))
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-count').textContent).toBe('Sent 1 · failed 1 · unconfirmed 0'))
    // In progress lists only the sent row, so the refused one drops out of the tally.
    fireEvent.click(screen.getByTestId('broadcast-preset-running'))
    expect(screen.queryByTestId('broadcast-row-gate')).toBeNull()
    expect(screen.getByTestId('broadcast-count').textContent).toBe('Sent 1 · failed 0 · unconfirmed 0')
  })

  it('heads the right column with the result once the list has been sent to', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    setup()
    expect(screen.getByTestId('broadcast-col-right').textContent).toBe('How it lands')
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-col-right').textContent).toBe('Result'))
    expect(screen.queryByTestId('broadcast-delivery-hint')).toBeNull()
  })

  it('says before a send that no running work is interrupted', () => {
    setup()
    expect(screen.getByTestId('broadcast-delivery-hint').textContent).toContain('No running work is interrupted')
  })

  it('names the retry after a refusal instead of a bare count', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-busy' ? { status: 'refused', body: {} } : { status: 'dispatched', body: {} })
    setup()
    expect(screen.getByTestId('broadcast-send').textContent).toBe('Send to 2')
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    await waitFor(() => expect(screen.getByTestId('broadcast-send').textContent).toBe('Retry 1 failed'))
  })

  it('lets the panel shrink and scroll inside the sidebar, so Send stays reachable on a phone', () => {
    setup()
    const cls = screen.getByTestId('broadcast-panel').className
    expect(cls).toContain('min-h-0')
    expect(cls).toContain('overflow-y-auto')
  })

  it('caps the right column so a narrow pane keeps the session title readable', () => {
    setup()
    const label = screen.getByText('Joins running turn')
    expect(label.className).toContain('max-w-[45%]')
    expect(label.className).not.toContain('whitespace-nowrap')
    expect(label.className).not.toContain('shrink-0')
  })

  it('opens an unconfirmed session from its row', async () => {
    sendTurn.mockImplementation(async ({ slot }: { slot: string }) =>
      slot === 'loop-busy' ? { status: 'response-late', body: {} } : { status: 'dispatched', body: {} })
    const { onOpenSession } = setup()
    fireEvent.change(screen.getByTestId('broadcast-message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('broadcast-send'))
    fireEvent.click(await screen.findByTestId('broadcast-open-loop-busy'))
    expect(onOpenSession).toHaveBeenCalledWith('loop-busy')
    expect(screen.queryByTestId('broadcast-open-loop-idle')).toBeNull()
  })
})
