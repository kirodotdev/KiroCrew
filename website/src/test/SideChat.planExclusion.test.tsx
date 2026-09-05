import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import reducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { renderWithProviders, createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideQueueCancel: vi.fn().mockResolvedValue({ ok: true, content: '', depth: 0 }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
    // The plan-dispatch transport (usePlanActionMutation goes through this).
    // Never wired into SideChat — that absence is exactly what this file pins.
    planAction: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import type { ChatMessage } from '../types'

const SLOT = 'side-plan-slot-1'
const initial = reducer(undefined, { type: '@@INIT' })
// The composer blocks sends while the gateway reads as offline, so the scene
// runs against a connected dashboard.
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }

// Header + stage line + protocol footer — the shape that makes parseOptions set
// isPlan on an assistant turn (same fixture family as the ChatPane/ChatPage
// dispatch tests).
const PLAN_TEXT = '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTIONS: Go | Go All | Cancel]'

// ONE source of truth for the side-buffer content: the store below renders it,
// and the premise pin derives from it — so the two cannot diverge on content
// or roles.
const SIDE_MESSAGES = [
  { role: 'user' as const, content: 'plan it', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
  { role: 'assistant' as const, content: PLAN_TEXT, ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
]

// The same role/cls mapping SideChat's transcript memo applies to the side
// buffer (SideChat.tsx — is_error absent and streaming=false here, so a
// non-user row maps to 'assistant').
const asTranscript: ChatMessage[] = SIDE_MESSAGES.map(m => {
  const role = m.role === 'user' ? 'user' : 'assistant'
  return { role, content: m.content, cls: `msg msg-${role}`, ts: m.ts }
})

function planStore() {
  return createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: SIDE_MESSAGES,
          lastRunId: 'r1',
          pending: false,
          streaming: false,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
        },
      },
    },
  })
}

// Pins the deliberate exclusion recorded at SideChat's deriveFollowUpOptions
// destructure (#6754; sibling issue #6057 covers the same drop in ChatEmbed):
// the side panel is NOT a plan-capable host. A side answer whose derivation yields
// followUpIsPlan=true still keeps its chips on the composer-draft path, and no
// plan action is ever dispatched from this surface.
describe('SideChat plan exclusion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('a plan-shaped chip edits the draft and never dispatches a plan action', async () => {
    // Premise pin: the rendered fixture MUST derive as a plan (user + settled
    // assistant). `asTranscript` is mapped from the SAME `SIDE_MESSAGES` array
    // the store renders, so this cannot silently degrade into a plain
    // follow-up test if the fixture or the plan grammar drifts.
    expect(deriveFollowUpOptions(asTranscript, false).followUpIsPlan).toBe(true)

    renderWithProviders(<SideChat slot={SLOT} />, { store: planStore() })

    const input = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(screen.getByText('Go')).toBeInTheDocument()

    // Single chip click. SideChat supplies onSend to FollowUpBar, so the click
    // is debounced ~220ms before it lands on onSelect (toggleOption).
    fireEvent.click(screen.getByText('Go'))

    // Composer-draft path: the pick lands in the side composer's draft, staying
    // amendable before send. This is the load-bearing assertion — a dispatch
    // branch would return before the draft edit, so wiring plan dispatch into
    // this handler reds this line.
    await waitFor(() => expect(input.value).toBe('Go'))

    // And no dispatch on the global client's plan-action transport.
    expect(api.planAction).not.toHaveBeenCalled()
    // Nor was a side turn sent — picking is an edit, not a submit.
    expect(api.sideTurn).not.toHaveBeenCalled()
  })
})

// An action-marked side answer carrying BOTH ordinary options and a close action, so
// the pin below can tell "no chip for the action" from "no chips at all".
const ACTION_TEXT =
  'All done.\n\n[OPTIONS: Alpha | Beta]\n[OPTION-ACTIONS: close=Nothing else, close this session]'

const ACTION_MESSAGES = [
  { role: 'user' as const, content: 'anything else?', ts: '2026-05-21T00:00:00Z', run_id: 'r2' },
  { role: 'assistant' as const, content: ACTION_TEXT, ts: '2026-05-21T00:00:01Z', run_id: 'r2' },
]

const asActionTranscript: ChatMessage[] = ACTION_MESSAGES.map(m => {
  const role = m.role === 'user' ? 'user' : 'assistant'
  return { role, content: m.content, cls: `msg msg-${role}`, ts: m.ts }
})

function actionStore() {
  return createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: ACTION_MESSAGES,
          lastRunId: 'r2',
          pending: false,
          streaming: false,
          openedAtTurnCount: 0,
          createdAt: '2026-05-21T00:00:00Z',
        },
      },
    },
  })
}

// Pins the action DROP at SideChat's destructure, sibling of the plan drop above: this
// surface wires no `onAction`, so that label as a chip would only send the sentence.
describe('SideChat action exclusion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('offers no chip for the action label, while ordinary options still render', () => {
    // Premise pin: the rendered fixture MUST derive an action, or the absence below is
    // about a label that was never offered and this test can never fail.
    const derived = deriveFollowUpOptions(asActionTranscript, false)
    expect(derived.followUpAction).not.toBeNull()
    const actionLabel = derived.followUpAction!.label
    expect(actionLabel.length).toBeGreaterThan(0)

    renderWithProviders(<SideChat slot={SLOT} />, { store: actionStore() })

    // Positive control: the bar DID render, so the absence is a fact about the action
    // rather than about a fixture that produced no chips at all.
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.queryByText(actionLabel)).toBeNull()
    // And nothing closed the side buffer off the back of a label that is not shown.
    expect(api.sideClose).not.toHaveBeenCalled()
  })
})
