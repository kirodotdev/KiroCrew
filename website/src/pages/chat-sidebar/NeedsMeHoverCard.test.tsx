/** The sidebar's needs-me hover card: when it opens, what it lists, and the two
 *  writes it can make (an approval decision on the strict slot route, and a
 *  quick reply through the composer's send). */
import { useRef } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage, ChatSlot } from '../../types'

const sendTurn = vi.hoisted(() => vi.fn())
vi.mock('../../chat-core/transport/sendTurn', () => ({ sendTurn, mintSendId: () => 'send-1' }))
const resolveAskAfterSend = vi.hoisted(() => vi.fn(async () => true))
vi.mock('../../lib/resolveAskAfterSend', () => ({ resolveAskAfterSend }))
// No shared-Crew revision read in these tests: the press-time base is simply unknown.
vi.mock('../../hooks/sharedCrew/sendRevisionSeed', () => ({ baseRevisionForPress: async () => undefined }))

const api = vi.hoisted(() => ({
  approveChatSlot: vi.fn(),
  resolveApproval: vi.fn(),
  sessionSummary: vi.fn(),
  chatSlotDetail: vi.fn(),
}))
vi.mock('../../api/client', async importActual => {
  const actual = await importActual<typeof import('../../api/client')>()
  return { ...actual, api }
})

import NeedsMeHoverLayer, { CLOSE_GRACE_MS, FINE_POINTER_QUERY, OPEN_DELAY_MS } from './NeedsMeHoverCard'
import { collectAsks, pickRecap } from './needsMeCard'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import { clearQuestionCard, setQuestionCard } from '../../store/chatSlice'
import { turnOnCrewModePreview } from '../../test/crewModePreview'
import { sendRaceNoticeFor } from '../../api/sendRace'

const LONG_COMMAND = "npx playwright test tests/auth --project=chromium --workers=2 --reporter=line --grep '@redirect' && git log --oneline -3"

function permission(over: Record<string, unknown> = {}): ChatMessage {
  return {
    role: 'permission', content: '🔧 Shell', cls: '',
    meta: { approval_id: 'req-1', mid: 'mid-1', tool_name: 'Shell', tool_purpose: 'Run the auth tests, then show the last commits', full_command: LONG_COMMAND, ...over },
  } as ChatMessage
}

const SLOTS: ChatSlot[] = [
  { key: 'gate', title: 'Fix login redirect', messages: 9, running: true, pending_approval: true, last_message: 'Ready to push.' },
  { key: 'blocked', title: 'Blocked only', messages: 4, running: true, pending_approval: true },
  { key: 'idle', title: 'Idle', messages: 3, running: false },
  { key: 'ask', title: 'Release notes', messages: 5, running: true, needs_input: true },
  { key: 'worker', title: 'Crew worker', messages: 6, running: true, pending_approval: true, crew_root: 'conductor-1' },
  { key: 'choice', title: 'Pick a plan', messages: 4, running: false, has_options: true, options: ['Upgrade both', 'Pin until next week'], last_message: 'Upgrade both now, or pin until next week?' },
] as ChatSlot[]

let fine = true
function stubMatchMedia() {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (q: string) => ({
      matches: q === FINE_POINTER_QUERY ? fine : false, media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }),
  })
}

function Sidebar() {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div ref={ref}>
      {SLOTS.map(s => (
        <div key={s.key} data-slot-key={s.key}>
          <div data-session-row={s.key} role="button" tabIndex={0} data-testid={`row-${s.key}`}>{s.title}</div>
        </div>
      ))}
      {/* A peer crew's row whose raw key collides with the local 'gate' slot. */}
      <div data-slot-key="gate"><div data-session-row="peer-1:gate" role="button" tabIndex={0} data-testid="row-peer">Peer gate</div></div>
      <NeedsMeHoverLayer rootRef={ref} />
    </div>
  )
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<Provider store={store}><QueryClientProvider client={qc}><Sidebar /></QueryClientProvider></Provider>)
}

function hover(key: string) {
  fireEvent.pointerOver(screen.getByTestId(`row-${key}`))
  act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS + 10) })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  fine = true
  stubMatchMedia()
  store.dispatch(sseSlots(SLOTS))
  store.dispatch(clearQuestionCard({ slot: 'ask' }))
  api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  api.chatSlotDetail.mockImplementation(async (slot: string) => ({
    key: slot, running: true, total: 2, has_more: false, next_before: 0,
    messages: slot === 'choice'
      ? [{ role: 'assistant', content: 'Two packages have major updates. Upgrade both now, or pin until next week?', cls: '' } as ChatMessage]
      : slot === 'blocked'
      ? [permission({ approval_id: 'req-9', mid: 'mid-9', deny_override_rule: 'rm-rf' })]
      : [permission()],
  }))
  api.approveChatSlot.mockResolvedValue({ ok: true })
  sendTurn.mockResolvedValue({ status: 'dispatched' })
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('NeedsMeHoverCard', () => {
  it('opens on hover for a needs-me row and lists the full command, with no Open control', async () => {
    mount()
    hover('gate')
    const card = await screen.findByTestId('needs-me-card')
    expect(card.getAttribute('role')).toBe('dialog')
    expect(screen.getByTestId('needs-me-command').textContent).toBe(LONG_COMMAND)
    expect(screen.getByTestId('needs-me-recap').textContent).toBe('Ready to push.')
    expect(screen.queryByRole('button', { name: /^open/i })).toBeNull()
    expect(screen.getByTestId('needs-me-purpose').textContent).toBe('Run the auth tests, then show the last commits')
    expect(screen.getByText('Replies go to the chat. Use Approve or Deny above.')).toBeTruthy()
    expect(screen.getByTestId('needs-me-decision-hint').textContent).toBe('Approve lets the agent do this once. Deny skips it.')
  })

  it('does not open a local card for a peer row whose key collides with a local slot', async () => {
    mount()
    hover('peer')
    await act(async () => { await Promise.resolve() })
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })

  it('opens for an idle session whose last reply asks for a choice', async () => {
    mount()
    hover('choice')
    const ask = await screen.findByText('Two packages have major updates. Upgrade both now, or pin until next week?')
    const row = ask.closest('[data-kind="reply"]')!
    expect(row.textContent).toContain('Upgrade both')
    expect(row.textContent).toContain('Pin until next week')
    expect(screen.getByTestId('needs-me-recap').textContent).toBe('No recap yet.')
  })

  it('shows a notice when the approvals cannot be loaded', async () => {
    api.chatSlotDetail.mockRejectedValue(new Error('503'))
    mount()
    hover('gate')
    await screen.findByText('Could not load the approvals. Click the session to see them.')
  })

  it('keeps the reply when the send gets no receipt in time', async () => {
    sendTurn.mockResolvedValue({ status: 'response-late' })
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'did this land?' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await screen.findByText('Not confirmed yet. Check the session before sending again.')
    expect((box as HTMLTextAreaElement).value).toBe('did this land?')
  })

  it('does not open when the pointer leaves the row before the delay', async () => {
    const { container } = mount()
    fireEvent.pointerOver(screen.getByTestId('row-gate'))
    fireEvent.pointerOver(container.firstElementChild!)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS + 10) })
    await act(async () => { await Promise.resolve() })
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
  })

  it('does not allow a second send after reopening mid-send', async () => {
    let finish: (v: unknown) => void = () => {}
    sendTurn.mockReturnValue(new Promise(r => { finish = r }))
    mount()
    hover('gate')
    let box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'once' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
    hover('gate')
    box = await screen.findByTestId('needs-me-reply')
    fireEvent.keyDown(box, { key: 'Enter' })
    // The send awaits the press-time revision before posting: let that settle.
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(sendTurn).toHaveBeenCalledTimes(1)
    await act(async () => { finish({ status: 'dispatched' }) })
  })

  it('a reply releases the open question card, as the composer send does', async () => {
    store.dispatch(setQuestionCard({ slot: 'ask', ask_id: 'a1', questions: [{ question: 'Which tone?', options: [{ label: 'Short' }] }] }))
    sendTurn.mockResolvedValue({ status: 'queued', body: { ok: true, queued: true } })
    mount()
    hover('ask')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'Short, please' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(resolveAskAfterSend).toHaveBeenCalledWith({ ok: true, queued: true }, 'a1', expect.any(Function), 'ask'))
  })

  it('does not bring back a sent reply when the card closed before the receipt', async () => {
    let finish: (v: unknown) => void = () => {}
    sendTurn.mockReturnValue(new Promise(r => { finish = r }))
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'sent once' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sendTurn).toHaveBeenCalled())
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
    await act(async () => { finish({ status: 'dispatched' }) })
    hover('gate')
    expect(((await screen.findByTestId('needs-me-reply')) as HTMLTextAreaElement).value).toBe('')
  })

  it('names the verdict when another tab answered first', async () => {
    const { ApiError } = await vi.importActual<typeof import('../../api/client')>('../../api/client')
    api.approveChatSlot.mockRejectedValue(new ApiError(409, 'already resolved', JSON.stringify({ code: 'already_resolved', decision: { decision_id: 'd1', outcome: 'rejected' } })))
    mount()
    hover('gate')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve Shell' }))
    await screen.findByText('Denied elsewhere')
  })

  it('a card reopened mid-send unlocks when that send settles', async () => {
    let finish: (v: unknown) => void = () => {}
    sendTurn.mockReturnValueOnce(new Promise(r => { finish = r }))
    mount()
    hover('gate')
    let box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'first' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
    hover('gate')
    box = await screen.findByTestId('needs-me-reply')
    await act(async () => { finish({ status: 'dispatched' }) })
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''))
    fireEvent.change(box, { target: { value: 'second' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(2))
  })

  it('a card reopened mid-send shows that send failing', async () => {
    let finish: (v: unknown) => void = () => {}
    sendTurn.mockReturnValueOnce(new Promise(r => { finish = r }))
    mount()
    hover('gate')
    let box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'will fail' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
    hover('gate')
    box = await screen.findByTestId('needs-me-reply')
    await act(async () => { finish({ status: 'refused', reason: 'The session is closed.' }) })
    await screen.findByText('The session is closed.')
    expect((box as HTMLTextAreaElement).value).toBe('will fail')
  })

  it('does not open for a row that owes nothing', () => {
    mount()
    hover('idle')
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
  })

  it('does not open on a touch device', async () => {
    fine = false
    mount()
    hover('gate')
    await act(async () => { await Promise.resolve() })
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })

  it('shows no card for a session that is only blocked by a safety rule', async () => {
    mount()
    hover('blocked')
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledWith('blocked', 80))
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })

  it('Approve posts the strict slot route naming the row, and the row reads Approved in place', async () => {
    mount()
    hover('gate')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve Shell' }))
    await screen.findByText('Approved')
    expect(api.approveChatSlot).toHaveBeenCalledTimes(1)
    const [slot, action, extra] = api.approveChatSlot.mock.calls[0]
    expect(slot).toBe('gate')
    expect(action).toBe('approved')
    expect(extra).toMatchObject({ origin: 'native', request_id: 'req-1', request_mid: 'mid-1' })
    expect(typeof extra.response_id).toBe('string')
    expect(api.resolveApproval).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Approve Shell' })).toBeNull()
  })

  it('Deny refuses this one call on the strict route', async () => {
    mount()
    hover('gate')
    fireEvent.click(await screen.findByRole('button', { name: 'Deny Shell' }))
    await screen.findByText('Denied')
    expect(api.approveChatSlot.mock.calls[0][1]).toBe('rejected_once')
    expect(api.approveChatSlot.mock.calls[0][2]).toMatchObject({ origin: 'native', request_mid: 'mid-1' })
  })

  it('shows a Crew Mode worker approval without decision buttons', async () => {
    turnOnCrewModePreview()
    mount()
    hover('worker')
    await screen.findByTestId('needs-me-command')
    expect(screen.queryByRole('button', { name: 'Approve Shell' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Deny Shell' })).toBeNull()
    expect(screen.getByText('Decide this on the conductor’s approval card.')).toBeTruthy()
  })

  it('keeps the card and the draft when a click inside it blurs to nothing', async () => {
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'half typed' } })
    fireEvent.focusOut(box, { relatedTarget: null })
    // Let any exit animation finish: a closed card would be gone after this.
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByTestId('needs-me-card')).toBeTruthy()
    expect((screen.getByTestId('needs-me-reply') as HTMLTextAreaElement).value).toBe('half typed')
  })

  it('lists a question card in full with no action button', async () => {
    store.dispatch(setQuestionCard({ slot: 'ask', ask_id: 'a1', questions: [{ question: 'Which tone should the notes use for the release?', options: [{ label: 'Short list' }, { label: 'Paragraphs' }] }] }))
    mount()
    hover('ask')
    await screen.findByText('Which tone should the notes use for the release?')
    await waitFor(() => expect(api.sessionSummary).toHaveBeenCalledWith('ask'))
    // No approval is pending, so the transcript is never fetched.
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
    const q = screen.getAllByTestId('needs-me-ask').find(el => el.dataset.kind === 'question')!
    expect(q.textContent).toContain('Short list')
    expect(q.querySelector('button')).toBeNull()
  })

  it('Enter in the quick reply sends to that slot through sendTurn', async () => {
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'push after the tests pass' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await screen.findByText('Sent.')
    expect(sendTurn).toHaveBeenCalledWith(expect.objectContaining({ message: 'push after the tests pass', slot: 'gate' }))
    expect(typeof sendTurn.mock.calls[0][0].race.idempotencyKey).toBe('string')
  })

  it('keeps text typed while the send was in flight', async () => {
    let finish: (v: unknown) => void = () => {}
    sendTurn.mockReturnValue(new Promise(r => { finish = r }))
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'first' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sendTurn).toHaveBeenCalled())
    fireEvent.change(box, { target: { value: 'second thought' } })
    await act(async () => { finish({ status: 'dispatched' }) })
    await screen.findByText('Sent.')
    expect((box as HTMLTextAreaElement).value).toBe('second thought')
  })

  it('keeps the reply and says so when another person sent first', async () => {
    sendTurn.mockResolvedValue({ status: 'refused', body: { code: 'send_race', current_revision: 3, winner: { principal: null } } })
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'my take' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await screen.findByText('Someone else replied first, so this reply was not sent.')
    // The composer's notice would offer to send the composer's draft instead: none is published.
    expect(sendRaceNoticeFor('gate')).toBeNull()
    expect((box as HTMLTextAreaElement).value).toBe('my take')
  })

  it('Shift+Enter does not send', async () => {
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'line one' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(sendTurn).not.toHaveBeenCalled()
  })

  it('Esc closes the card and returns focus to the row', async () => {
    mount()
    const row = screen.getByTestId('row-gate')
    row.focus()
    fireEvent.keyDown(row, { key: 'ArrowRight' })
    const box = await screen.findByTestId('needs-me-reply')
    await waitFor(() => expect(box).toHaveFocus())
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
    expect(row).toHaveFocus()
  })

  it('closes when the pointer leaves the sidebar', async () => {
    const { container } = mount()
    hover('gate')
    await screen.findByTestId('needs-me-card')
    fireEvent.pointerLeave(container.firstElementChild!)
    act(() => { vi.advanceTimersByTime(CLOSE_GRACE_MS + 10) })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
  })
})

describe('needsMeCard model', () => {
  it('drops a safety-rule refusal and a resolved row, keeps a row this card settled', () => {
    const asks = collectAsks([
      permission({ deny_override_rule: 'rm-rf' }),
      permission({ approval_id: 'old', mid: 'm-old', resolved: 'approved' }),
      permission({ approval_id: 'mine', mid: 'm-mine', resolved: 'approved' }),
    ], undefined, { settled: new Set(['mine\u0000m-mine']) })
    expect(asks).toHaveLength(1)
    expect(asks[0]).toMatchObject({ kind: 'approval', requestId: 'mine' })
  })

  it('never parses the display title for the tool name', () => {
    const [ask] = collectAsks([{ role: 'permission', content: '🔧 [cron] Looks like a tool', cls: '', meta: { approval_id: 'r', mid: 'm', full_command: 'ls' } } as ChatMessage], undefined)
    expect(ask).toMatchObject({ kind: 'approval', tool: '' })
  })

  it('recaps from the newest live intent, else the last reply', () => {
    const intent = (title: string, turn: number, state = 'in-progress') => ({ title, progress: ['Tests pass'], state, last_touched_turn: turn })
    const summary = { enabled: true, intents: [intent('Old', 1), intent('New goal', 5), intent('Dropped', 9, 'dropped')] }
    expect(pickRecap(summary as never, 'last')).toEqual({ text: 'New goal. Tests pass', fromSummary: true })
    expect(pickRecap(undefined, 'last line')).toEqual({ text: 'last line', fromSummary: false })
    expect(pickRecap(undefined, '')).toBeNull()
  })
})
