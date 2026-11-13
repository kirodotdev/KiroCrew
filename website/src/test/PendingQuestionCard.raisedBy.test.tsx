/** A pending question names the teammate whose turn asked it, in one word. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import PendingQuestionCard from '../components/PendingQuestionCard'
import { api } from '../api/client'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import { sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import type { PublicPrincipal, SharedDecision } from '../api/sharedCrew'

vi.mock('../api/client', () => ({
  api: {
    answerQuestion: vi.fn(),
    sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn(), sharedCrewViewer: vi.fn(), sharedCrewDecisions: vi.fn(),
  },
  ApiError: class extends Error {},
}))

// The card label waits on capabilities, then mode and people, then the viewer and decisions:
// each hop is its own query, so the waits that sit behind that chain get a longer bound.
const CHAIN_TIMEOUT_MS = 5000
const person = (id: string, name: string) => ({ id, kind: 'human', display: { name, login: '', captured_at: null } }) as PublicPrincipal
const bob = person('p_bob', 'Bob')
const alice = person('p_alice', 'Alice Example')
const ask = (initiator: PublicPrincipal): SharedDecision => ({
  decision_id: 'question:ask-1', kind: 'question', session_key: 'chat-1', state: 'pending', revision: 1,
  initiator, responder: null, outcome: '', resolved_at: null,
})

function mount(items: SharedDecision[]) {
  const store = createTestStore({
    chat: {
      activeSlot: 'chat-1', messages: [],
      pendingQuestions: { 'chat-1': { slot: 'chat-1', ask_id: 'ask-1', questions: [{ question: 'Pick one', options: [{ label: 'A' }, { label: 'B' }] }] } },
    },
  } as never)
  const view = renderWithProviders(<PendingQuestionCard slotKey="chat-1" onFallbackSend={vi.fn()} />, { store })
  // The card only reads the decisions cache, which the chat's collab strip fills.
  view.queryClient.setQueryData(sharedCrewKeys.decisions('chat-1'), { schema_version: 1, items })
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, presence: 'available', attribution: 'available', shared_crew_preview: true })
  vi.mocked(api.sharedCrewMode).mockResolvedValue({ schema_version: 1, mode: 'network', crew_id: 'c1' } as never)
  vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: alice, sources: ['observed'], is_viewer: false }] } as never)
  vi.mocked(api.sharedCrewViewer).mockResolvedValue(bob)
})

describe('PendingQuestionCard raised-by label', () => {
  it("names the teammate whose turn asked", async () => {
    mount([ask(alice)])
    const pill = await screen.findByTestId('raised-by', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(pill).toHaveTextContent(/^Raised by Alice Example$/)
  })

  it('names nobody when the viewer asked', async () => {
    const { queryClient } = mount([ask(bob)])
    await screen.findByText('Pick one')
    await waitFor(() => expect(queryClient.getQueryData(sharedCrewKeys.decisions('chat-1'))).toBeDefined())
    await waitFor(() => expect(queryClient.getQueryData(sharedCrewKeys.viewer)).toBeDefined())
    expect(screen.queryByTestId('raised-by')).toBeNull()
  })
})
