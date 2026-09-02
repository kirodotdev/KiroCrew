// Follow-up on a review: the panel's state contract.
//
// What matters here is not pixels but the two states the backend can be in, and
// that the UI never lies about which one it is. A follow-up resumes the reviewer's
// own session from disk, so:
//
//   * RESUMABLE     -> a button, because reopening would restore the reasoning.
//   * NOT RESUMABLE -> NO button and an explanation, because a session opened
//                      without that context answers confidently about a review it
//                      knows nothing about.
//
// It also pins the two-call open sequence (this app arms the resume, the
// dashboard's own endpoint creates the slot) and that a backend error is rendered
// from the catalog via its `code` — never as the server's English prose, which
// would land untranslated inside a localized page.
//
// And it pins who gets markdown in the stored history: the reviewer's answers do
// (it writes markdown), the user's own words and the raw reasoning dump do not
// (rendering those would show text nobody wrote).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import type { ChatState, ChatTurn } from '../apps/code-review-sage/lib/types'

const chatState = vi.fn()
const followupStart = vi.fn()
const createChatSlot = vi.fn()
const navigate = vi.fn()

class FakeSageApiError extends Error {
  code: string
  constructor(message: string, code: string) {
    super(message)
    this.code = code
  }
}

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    chatState: (...a: unknown[]) => chatState(...a),
    followupStart: (...a: unknown[]) => followupStart(...a),
  },
  SageApiError: FakeSageApiError,
}))

vi.mock('../api/client', () => ({
  api: { createChatSlot: (...a: unknown[]) => createChatSlot(...a) },
}))

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigate,
}))

const ReviewChat = (await import(
  '../apps/code-review-sage/components/ReviewChat')).default

function turn(over: Partial<ChatTurn> = {}): ChatTurn {
  return {
    role: 'reviewer',
    text: 'Because the caller retries on failure.',
    thinking: '',
    tools: [],
    refusals: [],
    ts: 1,
    ...over,
  }
}

function state(over: Partial<ChatState> = {}): ChatState {
  return {
    run_id: 'r1',
    change_id: 'c1',
    turns: [],
    resumable: true,
    reason: '',
    slot_key: 'sage-followup-abc123def456',
    followup_open: false,
    ...over,
  }
}

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <ReviewChat runId="r1" changeId="c1" />
    </QueryClientProvider>,
  )
}

async function expand() {
  mount()
  fireEvent.click(screen.getByRole('button', { name: /ask the reviewer/i }))
  await waitFor(() => expect(chatState).toHaveBeenCalled())
}

describe('ReviewChat', () => {
  beforeEach(() => {
    chatState.mockReset()
    followupStart.mockReset()
    createChatSlot.mockReset()
    navigate.mockReset()
  })

  it('is collapsed until asked for, and fetches nothing while collapsed', () => {
    chatState.mockResolvedValue(state())
    mount()
    // Most reports are read without questions; the findings are the substance.
    expect(chatState).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /follow-up session/i })).toBeNull()
  })

  it('offers to open a session while the review is resumable', async () => {
    chatState.mockResolvedValue(state())
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /open a follow-up session/i })).toBeTruthy())
    // It says what the button does, because "a chat" and "the session that
    // produced these findings" are different offers.
    expect(screen.getByText(/resumes the reviewer/i)).toBeTruthy()
  })

  it('offers nothing when the review kept no session', async () => {
    chatState.mockResolvedValue(state({
      resumable: false, reason: 'followup_not_recorded',
    }))
    await expand()
    await waitFor(() => expect(
      screen.getByText(/did not keep a reviewer session/i)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /open a follow-up session/i })).toBeNull()
  })

  it('distinguishes a lost transcript from one never kept', async () => {
    // Different causes, different remedies — collapsing them into one message
    // would tell a user to re-run a review that did run.
    chatState.mockResolvedValue(state({
      resumable: false, reason: 'followup_transcript_gone',
    }))
    await expand()
    await waitFor(() => expect(
      screen.getByText(/no longer on disk/i)).toBeTruthy())
    expect(screen.queryByText(/did not keep a reviewer session/i)).toBeNull()
  })

  it('offers nothing while the review is still running', async () => {
    // A first pass can be superseded by a second one in the same run, which
    // retires the descriptor — so a conversation opened mid-run would end up
    // pointing at findings the run replaced.
    chatState.mockResolvedValue(state({
      resumable: false, reason: 'followup_run_live',
    }))
    await expand()
    await waitFor(() => expect(
      screen.getByText(/still running/i)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /follow-up session/i })).toBeNull()
  })

  it('offers to CONTINUE when the conversation already exists', async () => {
    // A returning user must not be invited to "open" a conversation they already
    // had: the exchanges live in the Chat tab, so an "open" label reads as the
    // review having lost them.
    chatState.mockResolvedValue(state({ followup_open: true }))
    await expand()
    await waitFor(() => expect(screen.getByRole('button',
      { name: /continue the follow-up session/i })).toBeTruthy())
    expect(screen.getByText(/continues in the chat tab/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^open a follow-up session$/i }))
      .toBeNull()
  })

  it('continuing does not revert a session the user renamed or moved', async () => {
    // The slot endpoint addresses an existing slot by NAME and then re-pins
    // whatever title and folder it is given, so sending them on a continue would
    // silently undo the user's own rename and filing.
    chatState.mockResolvedValue(state({ followup_open: true }))
    followupStart.mockResolvedValue({
      ok: true,
      slot_key: 'sage-followup-abc123def456',
      agent: 'sage-reviewer',
      folder_id: 'fold1',
      title: 'followup-pr#42-fix the thing',
    })
    createChatSlot.mockResolvedValue({ key: 'sage-followup-abc123def456' })
    await expand()
    await waitFor(() => expect(screen.getByRole('button',
      { name: /continue the follow-up session/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button',
      { name: /continue the follow-up session/i }))
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())
    const call = createChatSlot.mock.calls[0]
    expect(call[0]).toBe('sage-followup-abc123def456')
    expect(call[1]).toBe('sage-reviewer')
    expect(call[5]).toBeUndefined()   // title
    expect(call[8]).toBeUndefined()   // folder_id
  })

  it('arms the resume, then creates the slot, then navigates to it', async () => {
    chatState.mockResolvedValue(state())
    followupStart.mockResolvedValue({
      ok: true,
      slot_key: 'sage-followup-abc123def456',
      agent: 'sage-reviewer',
      folder_id: 'fold1',
      title: 'followup-pr#42-fix the thing',
    })
    createChatSlot.mockResolvedValue({ key: 'sage-followup-abc123def456' })
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /open a follow-up session/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /open a follow-up session/i }))

    await waitFor(() => expect(followupStart).toHaveBeenCalledWith('r1', 'c1'))
    // The slot is created through the dashboard's own endpoint, carrying the
    // agent the review RAN as: a follow-up created under a different agent gets
    // a different tool surface than the session it is resuming.
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())
    const call = createChatSlot.mock.calls[0]
    expect(call[0]).toBe('sage-followup-abc123def456')
    expect(call[1]).toBe('sage-reviewer')
    expect(call[5]).toBe('followup-pr#42-fix the thing')
    expect(call[8]).toBe('fold1')
    await waitFor(() => expect(navigate).toHaveBeenCalledWith(
      '/chat?sid=sage-followup-abc123def456'))
  })

  it('does not create a slot when arming the resume is refused', async () => {
    // The order matters: a slot created for a resume that was refused is a
    // session with no review context and no way to tell.
    chatState.mockResolvedValue(state())
    followupStart.mockRejectedValue(
      new FakeSageApiError('the reviewer\'s session is no longer on disk',
        'followup_transcript_gone'))
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /open a follow-up session/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /open a follow-up session/i }))
    await waitFor(() => expect(screen.getByText(/no longer on disk/i)).toBeTruthy())
    expect(createChatSlot).not.toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('renders a backend failure from its code, not the server prose', async () => {
    chatState.mockResolvedValue(state())
    followupStart.mockRejectedValue(
      new FakeSageApiError('the review this belongs to was deleted',
        'chat_run_deleted'))
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /open a follow-up session/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /open a follow-up session/i }))
    await waitFor(() => expect(
      screen.getByText(/this review was deleted/i)).toBeTruthy())
    // The English sentence Python produced must never reach a localized page.
    expect(screen.queryByText(/belongs to was deleted/i)).toBeNull()
  })

  it('names the action it failed at, not a question nobody asked', async () => {
    // A createChatSlot failure is not a SageApiError, so it lands on the generic
    // string. The user clicked a button and asked nothing, so copy about an
    // unanswered question would be actively misleading.
    chatState.mockResolvedValue(state())
    followupStart.mockResolvedValue({
      ok: true, slot_key: 'k', agent: 'a', folder_id: '', title: 't',
    })
    createChatSlot.mockRejectedValue(new Error('no slot capacity'))
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /open a follow-up session/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /open a follow-up session/i }))
    await waitFor(() => expect(
      screen.getByText(/session could not be opened/i)).toBeTruthy())
    expect(screen.queryByText(/question could not be answered/i)).toBeNull()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('still shows what was discussed before follow-ups were sessions', async () => {
    chatState.mockResolvedValue(state({
      turns: [turn({ role: 'user', text: 'why?' }), turn()],
    }))
    await expand()
    await waitFor(() => expect(screen.getByText('why?')).toBeTruthy())
    expect(screen.getByText(/because the caller retries/i)).toBeTruthy()
    // Labelled, so it does not read as part of the session the button opens.
    expect(screen.getByText(/earlier questions/i)).toBeTruthy()
  })

  it('names the tools it looked at, inside the sentence', async () => {
    // The list is interpolated into the catalog string rather than concatenated
    // after a fragment, so this also pins that the placeholder actually resolves
    // instead of rendering a literal {{tools}}.
    chatState.mockResolvedValue(state({
      turns: [turn({ tools: ['Read useUpload.ts', 'Grep revokeObjectURL'] })],
    }))
    await expand()
    await waitFor(() => expect(
      screen.getByText(/Read useUpload\.ts, Grep revokeObjectURL/)).toBeTruthy())
    expect(screen.queryByText(/\{\{tools\}\}/)).toBeNull()
  })

  it('says so when an answer was limited by a refused tool', async () => {
    chatState.mockResolvedValue(state({
      turns: [turn({ refusals: ['run shell'] })],
    }))
    await expand()
    // A bounded answer must announce that it is bounded, or it reads as a
    // confident one.
    await waitFor(() => expect(
      screen.getByText(/this answer is limited/i)).toBeTruthy())
  })

  it('hides reasoning behind a disclosure rather than inlining it', async () => {
    chatState.mockResolvedValue(state({
      turns: [turn({ thinking: 'weighing the call sites' })],
    }))
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /reasoning/i })).toBeTruthy())
    expect(screen.queryByText(/weighing the call sites/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /reasoning/i }))
    expect(screen.getByText(/weighing the call sites/)).toBeTruthy()
  })

  it('renders the reviewer answer as markdown, not as literal syntax', async () => {
    // The reviewer answers in markdown -- real answers carry fenced code,
    // `identifiers`, bold and numbered steps. Rendered flat, that is a wall of
    // asterisks and backticks, which is what this locks against.
    chatState.mockResolvedValue(state({
      turns: [turn({
        text: 'The **flock** serializes `pod up`.\n\n1. writes CHECKOUT\n2. merges',
      })],
    }))
    await expand()
    await waitFor(() => expect(document.querySelector('strong')).toBeTruthy())
    expect(document.querySelector('strong')?.textContent).toBe('flock')
    expect(document.querySelector('code')?.textContent).toBe('pod up')
    expect(document.querySelector('ol')).toBeTruthy()
    // The markup must be consumed, not displayed.
    expect(document.body.textContent).not.toContain('**')
    expect(document.body.textContent).not.toContain('`pod up`')
  })

  it('echoes the user question verbatim, never as markdown', async () => {
    // Their own words, handed back. Reinterpreting them would show the user
    // something they did not type -- a question ABOUT `**kwargs` or a *glob*
    // must not come back bolded and italicized.
    const asked = 'Why does **kwargs break the *glob* pattern?'
    chatState.mockResolvedValue(state({
      turns: [turn({ role: 'user', text: asked })],
    }))
    await expand()
    await waitFor(() => expect(screen.getByText(asked)).toBeTruthy())
    expect(document.querySelector('strong')).toBeNull()
    expect(document.querySelector('em')).toBeNull()
  })
})
