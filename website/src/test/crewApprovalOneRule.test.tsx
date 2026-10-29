/**
 * Edge review R3 (G3, G4, Z1, Z2): one rule on every surface. For a Crew Mode
 * worker's pending tool approval, the ONLY place with decision buttons is the
 * conductor's approval card (`crewApprovalDecidableHere`).
 */
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { CREW_APPROVAL_SURFACES, crewApprovalDecidableHere } from '../lib/crewMode'
import { buildCommandCenter, crewWaitingApprovals, type CommandCenterSources } from '../pages/chat/command-center/model'
import AttentionCard from '../pages/chat/command-center/AttentionCard'
import type { ChatSlot } from '../types'
import { createTestStore } from './helpers'
import { crewModePreviewOn } from './crewModePreview'

const slot = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({ key, messages: 0, running: false, ...extra })
const waiting = {
  pending_approval: true,
  pending_approval_info: { origin: 'native', request_id: 'r1', request_mid: 'm1', tool: 'Shell', tool_input: 'printf ok' },
} as Partial<ChatSlot>
const crew = (): ChatSlot[] => [
  slot('root', { title: 'Paper Flags', crew_mode_root: true }),
  slot('w1', { title: 'Flag A', created_by: 'root', crew_root: 'root', ...waiting }),
  slot('w2', { title: 'Flag B', created_by: 'root', crew_root: 'root', ...waiting }),
  slot('plain', { title: 'Plain chat', ...waiting }),
]
const sources = (extra: Partial<CommandCenterSources>): CommandCenterSources => ({
  slots: crew(), root: 'root', subagents: {}, workflows: [], questions: [], approvals: [], ...extra,
})

describe('the one rule: a worker approval is decided only on the conductor card', () => {
  it('lists every surface and only the conductor card may decide a worker approval', () => {
    expect([...CREW_APPROVAL_SURFACES]).toEqual([
      'conductor_card', 'worker_main_pane', 'worker_split_pane', 'conductor_side_panel', 'session_dashboards',
    ])
    const worker = { crew_root: 'root' }
    for (const surface of CREW_APPROVAL_SURFACES) {
      expect(crewApprovalDecidableHere(worker, surface)).toBe(surface === 'conductor_card')
      // A session that is no worker keeps its usual controls everywhere.
      expect(crewApprovalDecidableHere({ crew_root: '' }, surface)).toBe(true)
      expect(crewApprovalDecidableHere(undefined, surface)).toBe(true)
    }
  })

  it('G4: the conductor side panel shows a worker approval as info that links to the card', () => {
    const model = buildCommandCenter(sources({}))
    const items = model.attention.filter(a => a.kind === 'approval')
    expect(items.map(a => [a.slot, a.crewCard, a.native])).toEqual([['w1', 'root', undefined], ['w2', 'root', undefined]])
  })

  it('Z2: All Dashboards shows the conductor as needing a tool decision while a worker waits', () => {
    const slots = crew()
    const fleet = slots.filter(s => !s.crew_root)
    const model = buildCommandCenter(sources({ root: null, slots: fleet, crewWaiting: crewWaitingApprovals(slots) }))
    const rollup = model.attention.find(a => a.slot === 'root')
    expect(rollup).toMatchObject({ kind: 'approval', crewCard: 'root', crewCount: 2 })
    expect(model.nodes.find(n => n.slot === 'root')?.state).toBe('needs_input')
    // Workers stay out; a plain chat keeps its own approval item.
    expect(model.attention.some(a => a.slot === 'w1' || a.slot === 'w2')).toBe(false)
    expect(model.attention.find(a => a.slot === 'plain')?.crewCard).toBeUndefined()
    // One request: count 1. None left: the conductor is no longer flagged.
    const one = slots.map(s => s.key === 'w2' ? { ...s, pending_approval: false } : s)
    expect(crewWaitingApprovals(one)).toEqual({ root: 1 })
    const none = slots.map(s => s.crew_root ? { ...s, pending_approval: false } : s)
    const cleared = buildCommandCenter(sources({ root: null, slots: fleet, crewWaiting: crewWaitingApprovals(none) }))
    expect(cleared.attention.some(a => a.slot === 'root')).toBe(false)
    expect(cleared.nodes.find(n => n.slot === 'root')?.state).toBe('idle')
  })

  it('Z2: another root is not flagged by this root\'s workers', () => {
    const slots = [...crew(), slot('root2', { crew_mode_root: true })]
    expect(crewWaitingApprovals(slots)).toEqual({ root: 2 })
  })

  it('G4/Z2: the attention card for a worker approval draws no Approve or Reject, only the card link', () => {
    const store = createTestStore({ dashboard: { slots: crew() } } as never)
    const qc = new QueryClient()
    const model = buildCommandCenter(sources({}))
    const item = model.attention.find(a => a.slot === 'w1')!
    render(<QueryClientProvider client={qc}><Provider store={store}><MemoryRouter>
      <AttentionCard item={item} title="Flag A" />
    </MemoryRouter></Provider></QueryClientProvider>)
    expect(screen.getByTestId('crew-approval-notice')).toHaveTextContent('Decide it on the approval card in Paper Flags.')
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull()
    expect(screen.getByRole('link', { name: /Open the approval card/ })).toHaveAttribute('href', '/chat?sid=root')
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
