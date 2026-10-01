import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/**
 * The shared Perpetual mode switch, read across a CHANGE of crewmate.
 *
 * The detail page remounts the hook per crew (`key={editing}`), but the
 * Crewmates page does not: it renders one
 * `useCrewPerpetualSwitch(activeMemberName)` and passes whichever crewmate the
 * reader selected. One mutation instance therefore outlives the crewmate it was
 * pressed for, and what is pinned here is that none of its state is read against
 * the next one -- a refusal, a "Saving…" and a "Saved" each belong to the
 * crewmate they were asked of, and a press on B is never described by A's
 * answer.
 *
 * The client is shared across the rerenders on purpose: a new one would remount
 * the queries and hide the defect, which lives in the mutation the host keeps.
 */

const H = vi.hoisted(() => ({
  members: vi.fn(),
  autonudgeList: vi.fn(),
  memberPerpetualSet: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    members: H.members,
    autonudgeList: H.autonudgeList,
    memberPerpetualSet: H.memberPerpetualSet,
  },
}))

import { CrewPerpetualControl, useCrewPerpetualSwitch } from './CrewPerpetualControl'

/** The Crewmates page's own shape: one hook, a crew name that changes under it. */
function Host({ crew }: { crew: string }) {
  const sw = useCrewPerpetualSwitch(crew, { poll: false })
  return (
    <div>
      <CrewPerpetualControl sw={sw} testIdPrefix="member-perpetual" />
      <span data-testid="refusal">{sw.refusalText}</span>
    </div>
  )
}

function mount(crew: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const view = (name: string) => (
    <QueryClientProvider client={client}>
      <Host crew={name} />
    </QueryClientProvider>
  )
  const { rerender } = render(view(crew))
  return { select: (name: string) => rerender(view(name)) }
}

const ROWS = [
  { name: 'Radar', slug: 'radar', slot_key: 'member-radar', running: false },
  { name: 'Nova', slug: 'nova', slot_key: 'member-nova', running: false },
]

beforeEach(() => {
  H.members.mockReset()
  H.autonudgeList.mockReset()
  H.memberPerpetualSet.mockReset()
  H.members.mockResolvedValue({ members: ROWS })
  H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
})
afterEach(cleanup)

const switchEl = () => screen.getByTestId('member-perpetual-switch').querySelector('[role="switch"]') as HTMLElement

describe('useCrewPerpetualSwitch across a crew change', () => {
  it("does not show A's refusal against B", async () => {
    H.memberPerpetualSet.mockRejectedValue(
      Object.assign(new Error('x'), {
        status: 409,
        body: JSON.stringify({ error: 'x', code: 'structured_monitor_not_convertible' }),
      }),
    )
    const { select } = mount('Radar')
    await waitFor(() => expect(screen.queryByTestId('member-perpetual-switch')).not.toBeNull())
    fireEvent.click(switchEl())
    await waitFor(() => expect(H.memberPerpetualSet).toHaveBeenCalledWith('radar', 'Radar', true))
    await waitFor(() => expect(screen.getByTestId('refusal').textContent).toMatch(/repeating task/))

    // The reader selects another crewmate. Nothing was asked of Nova, so
    // nothing may be said about Nova.
    select('Nova')
    await waitFor(() => expect(screen.getByTestId('refusal').textContent).toBe(''))
  })

  it("does not show A's pending press against B, and B's switch stays pressable", async () => {
    // A press that never answers: the worst case for leaked state, since
    // "Saving…" and the disabled switch would otherwise sit on B indefinitely.
    H.memberPerpetualSet.mockImplementation(() => new Promise(() => {}))
    const { select } = mount('Radar')
    await waitFor(() => expect(screen.queryByTestId('member-perpetual-switch')).not.toBeNull())
    fireEvent.click(switchEl())
    await waitFor(() => expect(screen.getByTestId('member-perpetual-control').getAttribute('aria-busy')).toBe('true'))
    expect(screen.getByTestId('member-perpetual-save-state').textContent).toMatch(/^Saving/)

    select('Nova')
    await waitFor(() => expect(screen.queryByTestId('member-perpetual-save-state')).toBeNull())
    expect(screen.getByTestId('member-perpetual-control').getAttribute('aria-busy')).toBeNull()
    expect(switchEl().getAttribute('aria-disabled')).toBeNull()
    // And a press on B addresses B, never the crewmate A's press was made for.
    fireEvent.click(switchEl())
    await waitFor(() => expect(H.memberPerpetualSet).toHaveBeenCalledWith('nova', 'Nova', true))
  })

  it('does not show A\'s "Saved" against B, nor bring it back on a return to A', async () => {
    H.memberPerpetualSet.mockResolvedValue({ ok: true })
    const { select } = mount('Radar')
    await waitFor(() => expect(screen.queryByTestId('member-perpetual-switch')).not.toBeNull())
    fireEvent.click(switchEl())
    await waitFor(() => expect(screen.getByTestId('member-perpetual-save-state').textContent).toBe('Saved'))

    select('Nova')
    await waitFor(() => expect(screen.queryByTestId('member-perpetual-save-state')).toBeNull())

    // Back to A well inside the "Saved" window: the press state was dropped on
    // the way out, so A does not re-announce a save the reader stopped watching.
    select('Radar')
    await waitFor(() => expect(screen.queryByTestId('member-perpetual-switch')).not.toBeNull())
    expect(screen.queryByTestId('member-perpetual-save-state')).toBeNull()
    expect(screen.getByTestId('refusal').textContent).toBe('')
  })
})
