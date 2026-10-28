import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, within } from '@testing-library/react'

import { api } from '../../../api/client'
import { SlotProvider } from '../../../providers/SlotContext'
import { createTestStore, renderWithProviders } from '../../../test/helpers'
import { kasTranscript } from '../../../test/kasFixtures'
import type { ChatMessage } from '../../../types'
import { KasEventMarkerRow, parseKasEventMarker } from './KasTurnMarker'
import { KasStepChat } from './KasStepChat'
import { getKasSelection, resetKasSelections } from './kasSelection'
import { kasSessionRowStatus } from './useKasSessionRowStatus'

afterEach(() => {
  vi.restoreAllMocks()
  resetKasSelections()
})

// ── 1. A step's note to its parent, in the step chat: #25470's outgoing card as
//       it is, with the agent and run in KAS's own meta line beside it. ──
describe('step chat — the note to the parent', () => {
  it('draws the outgoing session card and puts agent and run in the KAS meta line', async () => {
    vi.spyOn(api, 'kasStepTranscript').mockResolvedValue(
      kasTranscript({
        status: 'idle',
        agent: 'wf-coder',
        entries: [
          {
            kind: 'tool', id: 'p', ts: null, toolCallId: 'p', name: 'send_message', title: null, toolKind: null,
            status: 'completed', input: '{"message":"Pick a colour","severity":"warning"}', output: null,
          },
        ],
      }),
    )
    renderWithProviders(
      <KasStepChat workflowId="wf-1" nodeId="ask" stepRunning={false} chatTitle="Planner" agent="wf-coder" stepLabel="Ask" runName="capture" />,
    )
    const parent = await screen.findByTestId('kas-step-parent')
    const card = within(parent).getByTestId('session-message-card')
    expect(card).toHaveAttribute('data-direction', 'outgoing')
    expect(within(card).getByTestId('session-card-recipient')).toHaveTextContent('@Planner')
    expect(within(card).getByTestId('session-card-body')).toHaveTextContent('Pick a colour')
    expect(within(parent).getByTestId('kas-step-parent-meta')).toHaveTextContent('wf-coder · capture')
  })
})

// ── 2. A step note in the parent chat: a KAS row naming the step the way the
//       seed label names a sender (avatar + title link that opens the step). ──
const note = (over: Record<string, unknown> = {}) =>
  ({
    role: 'notice',
    content: '',
    cls: '',
    meta: { kas_event: { type: 'note', name: 'triage', severity: 'warning', message: 'need input', step: 'Review', agent: 'rev', workflowId: 'wf-9', nodeId: 'review', ...over } },
  }) as unknown as ChatMessage

describe('parent chat — a step note row', () => {
  it('keeps the run and step ids, so the title can open that step', () => {
    expect(parseKasEventMarker(note())).toMatchObject({ type: 'note', workflowId: 'wf-9', nodeId: 'review' })
  })

  it('names the step with the sessionColors avatar and a link that opens it in the run view', () => {
    const marker = parseKasEventMarker(note())
    if (!marker) throw new Error('no marker')
    const store = createTestStore()
    renderWithProviders(
      <SlotProvider slotId="chat-1">
        <KasEventMarkerRow marker={marker} />
      </SlotProvider>,
      { store },
    )
    const row = screen.getByTestId('kas-event-marker')
    expect(within(row).queryByTestId('session-message-card')).toBeNull()
    expect(within(row).getByTestId('session-seed-sender-avatar')).toHaveTextContent('R')
    expect(within(row).getByTestId('kas-note-meta')).toHaveTextContent('rev · triage')
    expect(within(row).getByTestId('kas-note-needs-you')).toBeInTheDocument()
    expect(within(row).getByTestId('kas-note-body')).toHaveTextContent('need input')
    fireEvent.click(within(row).getByTestId('kas-note-step-link'))
    expect(getKasSelection('chat-1')).toEqual({ workflowId: 'wf-9', nodeId: 'review' })
    expect(store.getState().chat.activityTab).toBe('workflows')
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('an info note has no pill, and a note without ids names the step as plain text', () => {
    const marker = parseKasEventMarker(note({ severity: 'info', workflowId: undefined, nodeId: undefined }))
    if (!marker) throw new Error('no marker')
    renderWithProviders(<KasEventMarkerRow marker={marker} />)
    expect(screen.queryByTestId('kas-note-needs-you')).toBeNull()
    expect(screen.queryByTestId('kas-note-step-link')).toBeNull()
    expect(screen.getByTestId('kas-note-step')).toHaveTextContent('Review')
  })
})

// ── 3. The sidebar row's KAS status, out of SessionRow.tsx. ──
describe('sidebar row — the KAS status', () => {
  const crew = { count: 1, name: 'crew run', phase: 'running' }
  const kas = { count: 2, name: 'triage', phase: 'Review', attention: true }

  it('fills the workflow line from KAS runs and flags a run parked on the person', () => {
    expect(kasSessionRowStatus(undefined, kas)).toEqual({ wf: { count: 2, name: 'triage', phase: 'Review' }, needsYou: true })
    expect(kasSessionRowStatus(undefined, { ...kas, attention: false }).needsYou).toBe(false)
    expect(kasSessionRowStatus(undefined, undefined)).toEqual({ wf: undefined, needsYou: false })
  })

  it('lets a Crew run outrank the KAS runs, needs-you included', () => {
    expect(kasSessionRowStatus(crew, kas)).toEqual({ wf: crew, needsYou: false })
  })
})
