// Post-review chat: the panel's state contract.
//
// What matters here is not pixels but the two states the backend can be in, and
// that the UI never lies about which one it is. A reviewer session is kept alive
// only for a bounded while, so:
//
//   * LIVE   -> a composer, because a question can actually be answered.
//   * CLOSED -> NO composer and an explanation, because an input that silently
//               fails is worse than no input, and the history is still intact.
//
// It also locks the two honesty details that are easy to lose: a degraded answer
// says it was degraded (a tool it wanted was refused), and a backend error is
// rendered from the catalog via its `code` — never as the server's English prose,
// which would land untranslated inside a localized page.
//
// And it pins who gets markdown: the reviewer's answers do (it writes markdown),
// the user's own words and the raw reasoning dump do not (rendering those would
// show text nobody wrote).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import type { ChatState, ChatTurn } from '../apps/code-review-sage/lib/types'

const chatState = vi.fn()
const chatAsk = vi.fn()
const chatClose = vi.fn()

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
    chatAsk: (...a: unknown[]) => chatAsk(...a),
    chatClose: (...a: unknown[]) => chatClose(...a),
  },
  SageApiError: FakeSageApiError,
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
    live: true,
    busy: false,
    turns: [],
    idle_ttl_secs: 1800,
    can_ask: true,
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
    chatAsk.mockReset()
    chatClose.mockReset()
  })

  it('is collapsed until asked for, and fetches nothing while collapsed', () => {
    chatState.mockResolvedValue(state())
    mount()
    // Most reports are read without questions; the findings are the substance.
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(chatState).not.toHaveBeenCalled()
  })

  it('offers a composer while the reviewer is still loaded', async () => {
    chatState.mockResolvedValue(state())
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    expect(screen.getByRole('button', { name: /send/i })).toBeTruthy()
  })

  it('shows history but NO composer once the session is gone', async () => {
    chatState.mockResolvedValue(state({
      live: false,
      turns: [turn({ role: 'user', text: 'why?' }), turn()],
    }))
    await expand()
    await waitFor(() => expect(screen.getByText('why?')).toBeTruthy())
    // The conversation is readable...
    expect(screen.getByText(/because the caller retries/i)).toBeTruthy()
    // ...but there is nothing to type into, and the reason is stated.
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.getByText(/no longer loaded/i)).toBeTruthy()
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
    await waitFor(() => expect(screen.getByText(/this answer is limited/i)).toBeTruthy())
  })

  it('hides reasoning behind a disclosure rather than inlining it', async () => {
    chatState.mockResolvedValue(state({
      turns: [turn({ thinking: 'weighing the call sites' })],
    }))
    await expand()
    await waitFor(() => expect(screen.getByRole('button', { name: /reasoning/i })).toBeTruthy())
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

  it('renders a backend failure from its code, not the server prose', async () => {
    chatState.mockResolvedValue(state())
    chatAsk.mockRejectedValue(
      new FakeSageApiError('no live reviewer session for this review',
        'chat_expired'))
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(
      screen.getByText(/review this pull request again/i)).toBeTruthy())
    // The English sentence Python produced must never reach a localized page.
    expect(screen.queryByText(/no live reviewer session/i)).toBeNull()
  })

  it('will not send an empty question, including via Enter', async () => {
    chatState.mockResolvedValue(state())
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: '   ' } })
    // The button is disabled, so a click cannot fire at all -- Enter is the path
    // that actually reaches submit(), and the only thing stopping a whitespace
    // question there is the guard inside it.
    fireEvent.keyDown(box, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    // Flush before asserting a NEGATIVE: mutate() invokes its mutationFn on a
    // later microtask, so asserting synchronously would pass even if the guard
    // were gone.
    await new Promise(r => setTimeout(r, 0))
    expect(chatAsk).not.toHaveBeenCalled()
  })

  it('sends on Enter and keeps Shift+Enter for a newline', async () => {
    chatState.mockResolvedValue(state())
    chatAsk.mockResolvedValue({ ok: true, turns: [] })
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    const box = screen.getByRole('textbox')
    fireEvent.change(box, { target: { value: 'why this one?' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    await new Promise(r => setTimeout(r, 0))
    expect(chatAsk).not.toHaveBeenCalled()
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(chatAsk).toHaveBeenCalledWith(
      'r1', 'c1', 'why this one?'))
  })

  it('does not offer a second question while one is in flight', async () => {
    chatState.mockResolvedValue(state({ busy: true }))
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    // The session refuses a concurrent prompt, so the UI must not invite one.
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  })

  it('hands back a half-typed question when the session lapses mid-sentence', async () => {
    // The poll can flip `live` between keystrokes. Unmounting the composer then
    // would take the unsent question with it -- users type slowly while reading
    // findings, so this is work lost with nowhere to resend it.
    chatState.mockResolvedValue(state())
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    fireEvent.change(screen.getByRole('textbox'),
      { target: { value: 'does the gallery hold the same memo?' } })

    // The session lapses on the next poll.
    chatState.mockResolvedValue(state({ live: false }))
    await waitFor(() => expect(
      screen.getByText(/no longer loaded/i)).toBeTruthy(), { timeout: 12000 })

    // The text survives, labelled as unsent, and is no longer an input.
    const box = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(box.value).toBe('does the gallery hold the same memo?')
    expect(box.readOnly).toBe(true)
    expect(screen.getByText(/not sent/i)).toBeTruthy()
    // No send affordance, because nothing can be sent.
    expect(screen.queryByRole('button', { name: /send/i })).toBeNull()
  })

  it('shows no unsent block when the composer was empty', async () => {
    chatState.mockResolvedValue(state({ live: false }))
    await expand()
    await waitFor(() => expect(screen.getByText(/no longer loaded/i)).toBeTruthy())
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByText(/not sent/i)).toBeNull()
  })

  it('will not offer a composer when tool use cannot be gated', async () => {
    // The session is LIVE but the turn would be refused: an agent spec's
    // allowedTools pre-approves tools that run with no permission event, so
    // without the override there is nothing to gate them with. Saying this before
    // a question is typed is the whole point.
    chatState.mockResolvedValue(state({ live: true, can_ask: false }))
    await expand()
    await waitFor(() => expect(screen.getByText(/turn on yolo/i)).toBeTruthy())
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: /send/i })).toBeNull()
    // Distinct from the reclaimed-session copy — different cause, different remedy.
    expect(screen.queryByText(/no longer loaded/i)).toBeNull()
  })

  it('distinguishes a reclaimed session from an ungatable one', async () => {
    chatState.mockResolvedValue(state({ live: false, can_ask: false }))
    await expand()
    await waitFor(() => expect(screen.getByText(/no longer loaded/i)).toBeTruthy())
    expect(screen.queryByText(/turn on yolo/i)).toBeNull()
  })

  it('renders a refused turn and a deleted run from their codes', async () => {
    chatState.mockResolvedValue(state())
    chatAsk.mockRejectedValue(
      new FakeSageApiError('tool use cannot be gated without the safety override',
        'chat_needs_override'))
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(
      screen.getByText(/turn on yolo to continue/i)).toBeTruthy())
    // Server prose must never reach a localized page.
    expect(screen.queryByText(/cannot be gated without/i)).toBeNull()
  })

  it('localizes a blocked tool, and shows the reason to nobody', async () => {
    // The code arrives with the policy reason appended, so an exact-match lookup
    // would miss it and fall through to the generic error.
    chatState.mockResolvedValue(state())
    chatAsk.mockRejectedValue(
      new FakeSageApiError('tool denied',
        'chat_tool_denied: denied command: ^curl\\s'))
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'fetch it' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(
      screen.getByText(/security policy blocks/i)).toBeTruthy())
    // The raw reason embeds a model-authored tool title — it stays server-side.
    expect(screen.queryByText(/\\^curl/)).toBeNull()
  })

  it('localizes an expiring authorization', async () => {
    chatState.mockResolvedValue(state())
    chatAsk.mockRejectedValue(
      new FakeSageApiError('nope', 'chat_override_expiring'))
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(
      screen.getByText(/runs out before an answer/i)).toBeTruthy())
  })

  it('distinguishes an authorization that lapsed mid-answer', async () => {
    chatState.mockResolvedValue(state())
    chatAsk.mockRejectedValue(
      new FakeSageApiError('nope', 'chat_override_lapsed'))
    await expand()
    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(
      screen.getByText(/ended while the reviewer was answering/i)).toBeTruthy())
  })

  it('can end the conversation to give the reviewer process back', async () => {
    chatState.mockResolvedValue(state())
    chatClose.mockResolvedValue({ ok: true, closed: true })
    await expand()
    await waitFor(() => expect(
      screen.getByRole('button', { name: /end conversation/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /end conversation/i }))
    await waitFor(() => expect(chatClose).toHaveBeenCalledWith('r1', 'c1'))
  })
})
